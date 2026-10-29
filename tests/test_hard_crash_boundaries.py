"""Kill real subprocesses at durable boundaries; only temp files represent effects.

These tests run actual executor/journal/outbox methods, never import/construct the
networked addon, never contact HA/Firebase, and never operate physical hardware.
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_reliability import CommandJournal, executor_type

COMMAND = {'id': 'hard-crash-fixture', 'entity_id': 'light.km_office_light',
           'action': 'toggle', 'params': {}}
CRASH_EXIT = 77


def durable_line(path, value):
    with Path(path).open('a') as output:
        output.write(json.dumps(value, sort_keys=True) + '\n')
        output.flush()
        os.fsync(output.fileno())


class LocalAcceptedResponse:
    """A fake cloud endpoint; persistence here precedes the HTTP200 response."""
    def __init__(self, path, payload):
        self.path, self.payload = path, payload
    async def __aenter__(self):
        durable_line(self.path, self.payload)
        return SimpleNamespace(status=200)
    async def __aexit__(self, *args):
        return False


class LocalSession:
    def __init__(self, cloud_file):
        self.cloud_file = cloud_file
    def post(self, url, **kwargs):
        if not url.endswith('/reportCommandResult'):
            raise AssertionError('Unexpected network-shaped request')
        payload = kwargs['json']
        return LocalAcceptedResponse(self.cloud_file, {
            'commandId': payload['commandId'], 'outcome': payload['outcome'],
            'trace': payload['trace']})


def addon_for(directory, journal_type=CommandJournal):
    directory = Path(directory)
    addon = executor_type()()
    addon.command_journal = journal_type(directory / 'journal.sqlite')
    addon.firebase_credentials = {'token': 'local-test-placeholder'}
    addon.firebase_project = 'offline-test-only'
    addon.hub_id = 'offline-fixture'
    addon.session = LocalSession(directory / 'cloud.jsonl')
    addon.running = True
    async def effect(domain, service, data):
        if (domain, service, data) != ('light', 'toggle', {'entity_id': COMMAND['entity_id']}):
            raise AssertionError('Unexpected fake effect')
        durable_line(directory / 'effects.jsonl', {'commandId': COMMAND['id']})
        return True
    addon.call_ha_service = effect
    return addon


async def child(directory, boundary):
    class CrashJournal(CommandJournal):
        def claim(self, command):
            claimed = super().claim(command)
            if boundary == 'after_claim' and claimed:
                os._exit(CRASH_EXIT)
            return claimed
        def complete(self, command_id, outcome):
            super().complete(command_id, outcome)
            if boundary == 'after_completion':
                os._exit(CRASH_EXIT)
        def acknowledge(self, command_id):
            if boundary == 'after_cloud_acceptance':
                os._exit(CRASH_EXIT)
            super().acknowledge(command_id)
    addon = addon_for(directory, CrashJournal)
    if boundary == 'after_effect':
        effect = addon.call_ha_service
        async def crash_after_effect(*args):
            await effect(*args)
            os._exit(CRASH_EXIT)
        addon.call_ha_service = crash_after_effect
    await addon.execute_command(COMMAND)
    if boundary == 'after_cloud_acceptance':
        await addon.command_ack_loop()
    raise AssertionError('Requested hard-crash boundary was not reached')


def lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()] if Path(path).exists() else []


class HardCrashBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, boundary, effects_before, recovered_outcome, cloud_before=0):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            # Separate interpreter and hard os._exit bypass every finally/close.
            result = await asyncio.to_thread(subprocess.run, [sys.executable, str(Path(__file__).resolve()),
                                     '--crash-child', str(directory), boundary],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, CRASH_EXIT, result.stderr)
            self.assertEqual(len(lines(directory / 'effects.jsonl')), effects_before)
            self.assertEqual(len(lines(directory / 'cloud.jsonl')), cloud_before)
            with sqlite3.connect((directory / 'journal.sqlite').as_uri() + '?mode=ro', uri=True) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                before = db.execute('SELECT fingerprint,outcome,acked,trace FROM commands WHERE id=?',
                                    (COMMAND['id'],)).fetchone()
            self.assertEqual(before[1], 'executing' if boundary in ['after_claim', 'after_effect'] else 'service_accepted')
            self.assertEqual(before[2], 0)
            original_trace = json.loads(before[3])
            addon = addon_for(directory)
            try:
                self.assertEqual(addon.command_journal.pending(), [(COMMAND['id'], recovered_outcome)])
                # Both concurrent transport duplicates and later serial redelivery
                # encounter the same durable ID; no fake service call is permitted.
                addon.call_ha_service = AsyncMock(side_effect=AssertionError('Replay after hard crash'))
                await asyncio.gather(*(addon.execute_command(dict(COMMAND)) for _ in range(8)))
                await addon.execute_command(dict(COMMAND))
                addon.call_ha_service.assert_not_awaited()
                self.assertEqual(addon.command_journal.pending(), [(COMMAND['id'], recovered_outcome)])
                self.assertEqual(addon.command_journal.trace(COMMAND['id'])['addonDispatchedAt'],
                                 original_trace['addonDispatchedAt'])
                after = addon.command_journal.db.execute('SELECT fingerprint FROM commands WHERE id=?',
                                                        (COMMAND['id'],)).fetchone()
                self.assertEqual(after[0], before[0])
                # Resume the actual acknowledgement loop. A repeated cloud receipt
                # is allowed; it must not execute HA or change the original trace.
                acknowledge = addon.command_journal.acknowledge
                def stop_after_ack(command_id):
                    acknowledge(command_id)
                    addon.running = False
                addon.command_journal.acknowledge = stop_after_ack
                with patch('asyncio.sleep', new=AsyncMock()):
                    await addon.command_ack_loop()
                self.assertEqual(addon.command_journal.pending(), [])
                self.assertEqual(len(lines(directory / 'effects.jsonl')), effects_before)
                receipts = lines(directory / 'cloud.jsonl')
                self.assertEqual(len(receipts), cloud_before + 1)
                self.assertEqual(receipts[-1]['outcome'], recovered_outcome)
                self.assertEqual(receipts[-1]['trace']['addonDispatchedAt'], original_trace['addonDispatchedAt'])
            finally:
                addon.command_journal.close()
            # A second restart preserves the acknowledged outcome, not a new job.
            journal = CommandJournal(directory / 'journal.sqlite')
            try:
                self.assertEqual(journal.pending(), [])
                self.assertFalse(journal.claim(COMMAND))
                self.assertEqual(journal.pending(), [(COMMAND['id'], recovered_outcome)])
            finally:
                journal.close()

    async def test_hard_exit_after_claim_does_not_invent_acceptance_or_blindly_execute(self):
        await self.exercise('after_claim', 0, 'unknown')

    async def test_hard_exit_after_fake_effect_preserves_uncertainty_and_suppresses_redelivery(self):
        await self.exercise('after_effect', 1, 'unknown')

    async def test_hard_exit_after_completion_recovers_accepted_outbox_without_reexecution(self):
        await self.exercise('after_completion', 1, 'service_accepted')

    async def test_hard_exit_after_cloud_acceptance_retries_receipt_but_not_effect(self):
        await self.exercise('after_cloud_acceptance', 1, 'service_accepted', cloud_before=1)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--crash-child':
        asyncio.run(child(sys.argv[2], sys.argv[3]))
    else:
        unittest.main()
