"""Confirmed dynamic scene definitions, persisted separately from command receipts."""
import json
import sqlite3
from pathlib import Path


class ManagedScenes:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=1)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS scenes (entity_id TEXT PRIMARY KEY, definition TEXT NOT NULL, active INTEGER NOT NULL)')
        self.db.commit()

    def save(self, entity_id, service_data):
        if not entity_id.startswith('scene.lapahub_') or not service_data.get('entities'):
            raise ValueError('Only explicit LapaHub scene definitions can be persisted')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO scenes VALUES (?, ?, 1)', (entity_id, json.dumps(service_data)))

    def suppress(self, entity_id):
        # Commit before HA deletion: a crash must never restore a deleted scene.
        with self.db:
            self.db.execute('UPDATE scenes SET active=0 WHERE entity_id=?', (entity_id,))

    def active(self):
        return {entity: json.loads(definition) for entity, definition in self.db.execute('SELECT entity_id, definition FROM scenes WHERE active=1')}

    def close(self):
        self.db.close()
