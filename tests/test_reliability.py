import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app'))
from reliability import CommandJournal, StatePublisher


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'commands.sqlite'
        self.journal = CommandJournal(self.path, capacity=2)
        self.command = {'id': 'trace-1', 'entity_id': 'light.office', 'action': 'turn_on', 'params': {}}
    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()
    def test_duplicate_does_not_dispatch_again(self):
        self.assertTrue(self.journal.claim(self.command))
        self.journal.complete('trace-1', 'service_accepted')
        self.assertFalse(self.journal.claim(self.command))
        self.assertEqual(self.journal.pending(), [('trace-1', 'service_accepted')])
    def test_receipt_survives_restart(self):
        self.journal.claim(self.command)
        self.journal.close()
        self.journal = CommandJournal(self.path)
        self.assertFalse(self.journal.claim(self.command))
        self.assertEqual(self.journal.pending(), [('trace-1', 'unknown')])
    def test_acknowledgement_survives_restart(self):
        self.journal.claim(self.command)
        self.journal.complete('trace-1', 'service_accepted')
        self.journal.close()
        self.journal = CommandJournal(self.path)
        self.assertEqual(self.journal.pending(), [('trace-1', 'service_accepted')])
        self.journal.acknowledge('trace-1')
        self.assertEqual(self.journal.pending(), [])
        self.assertFalse(self.journal.claim(self.command))
        self.assertEqual(self.journal.pending(), [('trace-1', 'service_accepted')])
    def test_trace_survives_restart_and_preserves_attempt_duration(self):
        self.journal.claim(self.command)
        self.journal.complete('trace-1', 'service_accepted')
        trace = self.journal.trace('trace-1')
        self.assertGreaterEqual(trace['addonAttemptDurationMs'], 0)
        self.assertLessEqual(trace['addonReceivedAt'], trace['addonCompletedAt'])
        self.journal.close()
        self.journal = CommandJournal(self.path)
        self.assertEqual(self.journal.trace('trace-1'), trace)
    def test_id_reuse_rejected_without_overwriting_result(self):
        self.journal.claim(self.command)
        self.journal.complete('trace-1', 'service_accepted')
        with self.assertRaises(ValueError):
            self.journal.claim({**self.command, 'action': 'turn_off'})
        self.assertEqual(self.journal.pending(), [('trace-1', 'service_accepted')])
    def test_capacity_refuses_untracked_execution(self):
        self.journal.claim(self.command)
        self.journal.claim({**self.command, 'id': 'trace-2'})
        with self.assertRaises(RuntimeError):
            self.journal.claim({**self.command, 'id': 'trace-3'})


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_cloud_does_not_block_ingestion_and_latest_state_wins(self):
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent = []
        async def publish(device, observed):
            sent.append((device['entity_id'], device['state']))
            entered.set()
            await release.wait()
            if len(sent) >= 2: completed.set()
        publisher = StatePublisher(publish, capacity=2)
        worker = asyncio.create_task(publisher.worker())
        try:
            publisher.offer({'entity_id': 'light.office', 'state': 'on'}, 1)
            await asyncio.wait_for(entered.wait(), 1)
            publisher.offer({'entity_id': 'light.office', 'state': 'off'}, 2)
            publisher.offer({'entity_id': 'light.office', 'state': 'on'}, 3)
            self.assertEqual(publisher.coalesced, 1)
            self.assertEqual(len(sent), 1)
            release.set()
            await asyncio.wait_for(completed.wait(), 1)
            self.assertEqual(sent, [('light.office', 'on'), ('light.office', 'on')])
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    async def test_two_entities_publish_concurrently_without_reordering_same_entity(self):
        release, both = asyncio.Event(), asyncio.Event()
        active, seen = set(), []
        async def publish(device, observed):
            entity = device['entity_id']
            self.assertNotIn(entity, active)
            active.add(entity); seen.append((entity, device['state']))
            if len(active) == 2: both.set()
            await release.wait()
            active.remove(entity)
        publisher = StatePublisher(publish)
        workers = [asyncio.create_task(publisher.worker()) for _ in range(3)]
        try:
            publisher.offer({'entity_id': 'a', 'state': 'on'}, 1)
            publisher.offer({'entity_id': 'b', 'state': 'off'}, 1)
            await asyncio.wait_for(both.wait(), 1)
            publisher.offer({'entity_id': 'a', 'state': 'off'}, 2)
            self.assertEqual(len(seen), 2)
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertIn(('a', 'off'), seen)
        finally:
            for worker in workers: worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
    async def test_failed_publication_retries_latest_state(self):
        sent, completed = [], asyncio.Event()
        async def publish(device, observed):
            sent.append(device['state'])
            if len(sent) == 1:
                publisher.offer({'entity_id': 'office', 'state': 'off'}, 2)
                return False
            completed.set()
            return True
        publisher = StatePublisher(publish)
        with patch('asyncio.sleep', new=AsyncMock()):
            worker = asyncio.create_task(publisher.worker())
            try:
                publisher.offer({'entity_id': 'office', 'state': 'on'}, 1)
                await asyncio.wait_for(completed.wait(), 1)
                self.assertEqual(sent, ['on', 'off'])
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    async def test_queue_is_bounded(self):
        publisher = StatePublisher(None, capacity=2)
        for index in range(100): publisher.offer({'entity_id': str(index)}, index)
        self.assertEqual(len(publisher.pending), 2)
        self.assertEqual(publisher.dropped, 98)



# Execute the actual addon methods without installing HA/network dependencies.
# Only the external service boundary is mocked; executor/journal wiring is real.
import ast
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

def executor_type():
    source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
    module = ast.parse(source.read_text())
    addon = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'LapaHubAddon')
    methods = [node for node in addon.body if isinstance(node, ast.AsyncFunctionDef) and node.name in {
        'execute_command', '_execute_command_once', 'report_command_result', 'command_ack_loop'}]
    namespace = {'asyncio': asyncio, 'datetime': datetime, 'timezone': timezone,
                 'logger': logging.getLogger('addon-test'),
                 'aiohttp': SimpleNamespace(ClientTimeout=lambda **kwargs: kwargs)}
    for method in methods:
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), namespace)
    return type('ActualAddonExecutor', (), {method.name: namespace[method.name] for method in methods})


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addon = executor_type()()
        self.addon.command_journal = CommandJournal(Path(self.tmp.name) / 'journal.sqlite')
        self.addon.call_ha_service = AsyncMock(return_value=True)
        self.command = {'id': 'trace-real', 'entity_id': 'light.office', 'action': 'turn_on', 'params': {}}
    async def asyncTearDown(self):
        self.addon.command_journal.close()
        self.tmp.cleanup()
    async def test_redelivery_calls_ha_only_once(self):
        await self.addon.execute_command(self.command)
        await self.addon.execute_command(self.command)
        self.addon.call_ha_service.assert_awaited_once_with('light', 'turn_on', {'entity_id': 'light.office'})
        self.assertEqual(self.addon.command_journal.pending(), [('trace-real', 'service_accepted')])
    async def test_expired_instruction_never_reaches_ha(self):
        await self.addon.execute_command({**self.command, 'expires_at': 0})
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(self.addon.command_journal.pending(), [('trace-real', 'expired')])
    async def test_uncertain_service_exception_does_not_replay(self):
        self.addon.call_ha_service.side_effect = TimeoutError('response lost')
        await self.addon.execute_command(self.command)
        await self.addon.execute_command(self.command)
        self.assertEqual(self.addon.call_ha_service.await_count, 1)
        self.assertEqual(self.addon.command_journal.pending(), [('trace-real', 'unknown')])
    async def test_failed_service_response_preserves_uncertain_outcome(self):
        self.addon.call_ha_service.return_value = False
        await self.addon.execute_command(self.command)
        self.assertEqual(self.addon.command_journal.pending(), [('trace-real', 'unknown')])
    async def test_scene_and_automation_targets_are_read_from_callable_envelope(self):
        self.addon.trigger_scene = AsyncMock(return_value=True)
        self.addon.delete_scene = AsyncMock(return_value=True)
        await self.addon.execute_command({'id': 'scene-activate', 'action': 'scene.turn_on', 'params': {'entity_id': 'scene.evening'}})
        self.addon.trigger_scene.assert_awaited_once_with('scene.evening')
        await self.addon.execute_command({'id': 'scene-delete', 'action': 'scene.delete', 'params': {'entity_id': 'scene.evening'}})
        self.addon.delete_scene.assert_awaited_once_with('scene.evening')
        await self.addon.execute_command({'id': 'automation-trigger', 'action': 'automation.trigger', 'params': {'entity_id': 'automation.office'}})
        self.addon.call_ha_service.assert_awaited_once_with('automation', 'trigger', {'entity_id': 'automation.office'})
    async def test_root_command_cannot_redirect_entity_through_service_data(self):
        await self.addon.execute_command({**self.command, 'params': {'entity_id': 'light.other'}})
        self.addon.call_ha_service.assert_awaited_once_with('light', 'turn_on', {'entity_id': 'light.office'})
    async def test_http_error_is_not_an_acknowledgement(self):
        class Response:
            status = 500
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        self.addon.firebase_credentials = {'token': 'test-token'}
        self.addon.firebase_project = 'demo-lapahub'
        self.addon.hub_id = 'test-hub'
        self.addon.session = SimpleNamespace(post=lambda *args, **kwargs: Response())
        with self.assertRaisesRegex(RuntimeError, 'HTTP 500'):
            await self.addon.report_command_result('trace-real', 'service_accepted')
    async def test_failed_ack_is_retained_then_retried_without_ha_execution(self):
        await self.addon.execute_command(self.command)
        self.addon.running = True
        attempts = []
        async def report(command_id, outcome):
            attempts.append((command_id, outcome))
            if len(attempts) == 1: raise RuntimeError('HTTP 500')
            self.addon.running = False
        self.addon.report_command_result = report
        with patch('asyncio.sleep', new=AsyncMock()):
            await self.addon.command_ack_loop()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.addon.command_journal.pending(), [])
        self.assertEqual(self.addon.call_ha_service.await_count, 1)

if __name__ == '__main__': unittest.main()
