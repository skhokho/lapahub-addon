"""Version-matched HA REST config contract and durable command behavior."""
import ast
import asyncio
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app'))
from reliability import CommandExpired, CommandJournal, check_command_deadline


def actual_type():
    source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
    addon = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'LapaHubAddon')
    names = {'_write_automation_config', 'create_automation', 'delete_automation', 'execute_command', '_execute_command_once'}
    methods = [n for n in addon.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    ns = dict(asyncio=asyncio, logger=logging.getLogger('automation-test'), datetime=datetime, timezone=timezone,
              CommandExpired=CommandExpired, check_command_deadline=check_command_deadline,
              HA_BASE_URL='http://supervisor/core', SUPERVISOR_TOKEN='test-token',
              aiohttp=SimpleNamespace(ClientTimeout=lambda **kw: kw))
    for method in methods:
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), ns)
    return type('ActualAutomationMethods', (), {m.name: ns[m.name] for m in methods})


class AutomationConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addon = actual_type()()
        self.addon.command_journal = CommandJournal(Path(self.tmp.name) / 'journal.sqlite')
        self.response = SimpleNamespace(status=200, json=AsyncMock(return_value={'result': 'ok'}))
        self.context = Mock()
        self.context.__aenter__ = AsyncMock(return_value=self.response)
        self.context.__aexit__ = AsyncMock(return_value=False)
        self.addon.session = SimpleNamespace(post=Mock(return_value=self.context), delete=Mock(return_value=self.context))
        self.addon.call_ha_service = AsyncMock(return_value=True)
        self.addon.log_activity = Mock()
    async def asyncTearDown(self):
        self.addon.command_journal.close()
        self.tmp.cleanup()
    async def test_create_and_delete_use_official_rest_contract(self):
        self.assertTrue(await self.addon.create_automation('lapahub_office', {'alias': 'Office'}))
        args, kw = self.addon.session.post.call_args
        self.assertEqual(args, ('http://supervisor/core/api/config/automation/config/lapahub_office',))
        self.assertEqual(kw['json'], {'alias': 'Office'})
        self.assertEqual(kw['headers']['Authorization'], 'Bearer test-token')
        self.assertTrue(await self.addon.delete_automation('lapahub_office'))
        self.assertNotIn('json', self.addon.session.delete.call_args.kwargs)
    async def test_http_success_without_config_acceptance_is_not_success(self):
        self.response.json.return_value = {'success': True}
        self.assertFalse(await self.addon.create_automation('lapahub_office', {}))
        self.response.status = 403
        self.assertFalse(await self.addon.delete_automation('lapahub_office'))
    async def test_imported_or_path_ids_are_rejected_before_network(self):
        for key in ['office', 'lapahub_x/../other', 'lapahub_x%2fother', 'lapahub_X']:
            with self.assertRaises(ValueError):
                await self.addon.delete_automation(key)
        self.addon.session.delete.assert_not_called()
    async def test_expired_configuration_never_reaches_network(self):
        with patch('reliability.time.time', return_value=102):
            with self.assertRaises(CommandExpired):
                await self.addon.create_automation('lapahub_office', {}, expires_at=101)
        self.addon.session.post.assert_not_called()
    async def test_lost_response_records_unknown_and_duplicate_does_not_retry(self):
        self.response.json.side_effect = TimeoutError('response lost after persistence')
        command = {'id': 'lost-config', 'action': 'automation.create', 'params': {'automation_id': 'lapahub_office', 'config': {'alias': 'Office'}}}
        await self.addon.execute_command(command)
        await self.addon.execute_command(command)
        self.assertEqual(self.addon.command_journal.pending(), [('lost-config', 'unknown')])
        self.addon.session.post.assert_called_once()
    async def test_manual_trigger_does_not_bypass_conditions(self):
        await self.addon.execute_command({'id': 'trigger', 'action': 'automation.trigger', 'params': {'entity_id': 'automation.lapahub_office'}})
        self.addon.call_ha_service.assert_awaited_once_with('automation', 'trigger', {'entity_id': 'automation.lapahub_office', 'skip_condition': False})

if __name__ == '__main__':
    unittest.main()
