"""Bounded state publication and durable command receipts. No external dependencies."""
import asyncio
import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path


class CommandJournal:
    """At-most-once dispatch per ID, with a durable acknowledgement outbox.

    A crash between the recorded claim and completion has an unknown outcome.
    It must never cause a blind replay of a potentially non-idempotent HA action.
    """
    def __init__(self, path, capacity=10000):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=1)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.capacity = capacity
        self.db.execute('CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, outcome TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)')
        with self.db:
            self.db.execute("UPDATE commands SET outcome='unknown', acked=0 WHERE outcome='executing'")

    def claim(self, command):
        command_id = command.get('id')
        if not isinstance(command_id, str) or not command_id or len(command_id) > 200:
            raise ValueError('A stable command ID is required')
        payload = {key: command.get(key) for key in ('entity_id', 'action', 'params')}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.db:
            row = self.db.execute('SELECT fingerprint, outcome FROM commands WHERE id=?', (command_id,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError('Command ID reused with a different payload')
                # Redelivery also retries an acknowledgement lost after cloud commit.
                self.db.execute('UPDATE commands SET acked=0 WHERE id=?', (command_id,))
                return False
            self.db.execute('DELETE FROM commands WHERE acked=1 AND updated<?', (time.time() - 7 * 86400,))
            if self.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0] >= self.capacity:
                raise RuntimeError('Command journal full; refusing untracked execution')
            self.db.execute('INSERT INTO commands VALUES (?, ?, ?, 0, ?)', (command_id, digest, 'executing', time.time()))
        return True

    def complete(self, command_id, outcome):
        if outcome not in ('service_accepted', 'failed', 'expired', 'unknown'):
            raise ValueError('Invalid command outcome')
        with self.db:
            self.db.execute('UPDATE commands SET outcome=?, acked=0, updated=? WHERE id=?', (outcome, time.time(), command_id))

    def pending(self, limit=20):
        return self.db.execute("SELECT id, outcome FROM commands WHERE acked=0 AND outcome!='executing' ORDER BY updated LIMIT ?", (limit,)).fetchall()

    def acknowledge(self, command_id):
        with self.db:
            self.db.execute('UPDATE commands SET acked=1 WHERE id=?', (command_id,))

    def close(self):
        self.db.close()


class StatePublisher:
    """Coalesce queued states per entity; never publish one entity concurrently.

    A fixed worker count bounds network concurrency. Capacity is a hard bound on
    queued entities. Overflow evicts the oldest queued state and increments a
    visible counter; periodic full sync remains the reconciliation path.
    """
    def __init__(self, publish, capacity=1024):
        self.publish = publish
        self.capacity = capacity
        self.pending = OrderedDict()
        self.active = set()
        self.ready = asyncio.Event()
        self.dropped = 0
        self.coalesced = 0

    def offer(self, device, observed_at):
        entity = device['entity_id']
        if entity in self.pending:
            self.coalesced += 1
        elif len(self.pending) >= self.capacity:
            self.pending.popitem(last=False)
            self.dropped += 1
        self.pending[entity] = (device, observed_at)
        self.ready.set()

    async def worker(self):
        while True:
            await self.ready.wait()
            item = next(((key, value) for key, value in self.pending.items() if key not in self.active), None)
            if item is None:
                self.ready.clear()
                continue
            entity, (device, observed_at) = item
            del self.pending[entity]
            self.active.add(entity)
            try:
                await self.publish(device, observed_at)
            finally:
                self.active.remove(entity)
                if self.pending:
                    self.ready.set()
                else:
                    self.ready.clear()
