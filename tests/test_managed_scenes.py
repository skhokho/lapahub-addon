import ast
import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app'))
from managed_scenes import ManagedScenes
from reliability import CommandExpired, CommandJournal, check_command_deadline


def actual_scene_type():
    source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
    addon = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.ClassDef) and node.name == 'LapaHubAddon')
    methods = [node for node in addon.body if isinstance(node, ast.AsyncFunctionDef) and node.name in {
        'create_scene', 'delete_scene', 'trigger_scene', 'restore_managed_scenes',
        'execute_command', '_execute_command_once'}]
    namespace = {'asyncio': asyncio, 'logger': logging.getLogger('scene-test'),
                 'datetime': datetime, 'timezone': timezone,
                 'CommandExpired': CommandExpired, 'check_command_deadline': check_command_deadline}
    for method in methods:
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), namespace)
    return type('ActualSceneMethods', (), {method.name: namespace[method.name] for method in methods})


class ManagedSceneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'scenes.sqlite'
        self.addon = actual_scene_type()()
        self.addon.managed_scenes = ManagedScenes(self.path)
        self.addon.command_journal = CommandJournal(Path(self.tmp.name) / 'journal.sqlite')
        self.addon.scene_lock = asyncio.Lock()
        self.addon.call_ha_service = AsyncMock(return_value=True)
        self.addon.get_ha_states = AsyncMock(return_value=[{'entity_id': 'light.office', 'state': 'off'}])
        self.addon.log_activity = lambda *args: None
        self.definition = {'light.office': {'state': 'off', 'brightness': 100}}
    async def asyncTearDown(self):
        self.addon.managed_scenes.close()
        self.addon.command_journal.close()
        self.tmp.cleanup()
    async def test_confirmed_definition_survives_restart_and_restore_never_activates_devices(self):
        await self.addon.create_scene('scene.lapahub_office', entities=self.definition)
        self.addon.managed_scenes.close()
        self.addon.managed_scenes = ManagedScenes(self.path)
        self.addon.call_ha_service.reset_mock()
        await self.addon.restore_managed_scenes([{'entity_id': 'light.office'}])
        self.addon.call_ha_service.assert_awaited_once_with('scene', 'create', {'scene_id': 'lapahub_office', 'entities': self.definition})
    async def test_existing_scene_is_not_recreated(self):
        await self.addon.create_scene('office', entities=self.definition)
        self.addon.call_ha_service.reset_mock()
        await self.addon.restore_managed_scenes([{'entity_id': 'scene.lapahub_office'}])
        self.addon.call_ha_service.assert_not_awaited()
    async def test_activation_after_reload_restores_definition_before_one_turn_on(self):
        await self.addon.create_scene('office', entities=self.definition)
        self.addon.call_ha_service.reset_mock()
        await self.addon.trigger_scene('scene.lapahub_office')
        self.assertEqual([call.args[:2] for call in self.addon.call_ha_service.await_args_list], [('scene', 'create'), ('scene', 'turn_on')])
    async def test_deleted_scene_is_not_restored_even_if_delete_response_is_lost(self):
        await self.addon.create_scene('office', entities=self.definition)
        self.addon.call_ha_service.side_effect = TimeoutError('lost response')
        with self.assertRaises(TimeoutError): await self.addon.delete_scene('scene.lapahub_office')
        self.addon.managed_scenes.close()
        self.addon.managed_scenes = ManagedScenes(self.path)
        self.addon.call_ha_service.reset_mock(side_effect=True)
        await self.addon.restore_managed_scenes([{'entity_id': 'light.office'}])
        self.addon.call_ha_service.assert_not_awaited()
    async def test_uncertain_creation_does_not_replace_last_confirmed_definition(self):
        await self.addon.create_scene('office', entities=self.definition)
        self.addon.call_ha_service.return_value = False
        await self.addon.create_scene('office', entities={'light.office': {'state': 'on'}})
        self.assertEqual(self.addon.managed_scenes.active()['scene.lapahub_office']['entities'], self.definition)
    async def test_snapshot_is_resolved_once_and_persisted_as_explicit_states(self):
        await self.addon.create_scene('snapshot', snapshot_entities=['light.office'])
        self.assertEqual(self.addon.managed_scenes.active()['scene.lapahub_snapshot']['entities'], {'light.office': {'state': 'off'}})

    async def test_imported_scene_cannot_be_silently_replaced_or_deleted(self):
        with self.assertRaisesRegex(ValueError, 'Imported HA scenes'):
            await self.addon.create_scene('scene.evening', entities=self.definition)
        with self.assertRaisesRegex(ValueError, 'Imported HA scenes'):
            await self.addon.delete_scene('scene.evening')
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(self.addon.managed_scenes.active(), {})

    async def test_activation_expiring_during_lookup_never_reaches_ha(self):
        await self.addon.create_scene('office', entities=self.definition)
        self.addon.call_ha_service.reset_mock()
        with patch('reliability.time.time', return_value=100) as clock:
            async def states_after_deadline():
                clock.return_value = 102
                return [{'entity_id': 'light.office'}]
            self.addon.get_ha_states.side_effect = states_after_deadline
            await self.addon.execute_command({'id': 'expired-lookup', 'action': 'scene.turn_on',
                'params': {'entity_id': 'scene.lapahub_office'}, 'expires_at': 101})
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(self.addon.command_journal.pending(), [('expired-lookup', 'expired')])

    async def test_activation_expiring_during_restore_never_turns_on_or_replays(self):
        await self.addon.create_scene('office', entities=self.definition)
        self.addon.call_ha_service.reset_mock()
        command = {'id': 'expired-restore', 'action': 'scene.turn_on',
                   'params': {'entity_id': 'scene.lapahub_office'}, 'expires_at': 101}
        with patch('reliability.time.time', return_value=100) as clock:
            async def restore_after_deadline(*args):
                clock.return_value = 102
                return True
            self.addon.call_ha_service.side_effect = restore_after_deadline
            await self.addon.execute_command(command)
            await self.addon.execute_command(command)
        self.addon.call_ha_service.assert_awaited_once_with('scene', 'create', {
            'scene_id': 'lapahub_office', 'entities': self.definition})
        self.assertEqual(self.addon.command_journal.pending(), [('expired-restore', 'expired')])

    async def test_delete_expiring_while_waiting_does_not_suppress_or_dispatch(self):
        await self.addon.create_scene('office', entities=self.definition)
        self.addon.call_ha_service.reset_mock()
        with patch('reliability.time.time', return_value=100) as clock:
            await self.addon.scene_lock.acquire()
            pending = asyncio.create_task(self.addon.execute_command({
                'id': 'expired-delete', 'action': 'scene.delete',
                'params': {'entity_id': 'scene.lapahub_office'}, 'expires_at': 101}))
            await asyncio.sleep(0)
            clock.return_value = 102
            self.addon.scene_lock.release()
            await pending
        self.addon.call_ha_service.assert_not_awaited()
        self.assertIn('scene.lapahub_office', self.addon.managed_scenes.active())
        self.assertEqual(self.addon.command_journal.pending(), [('expired-delete', 'expired')])

    async def test_snapshot_expiring_during_lookup_does_not_create(self):
        with patch('reliability.time.time', return_value=100) as clock:
            async def states_after_deadline():
                clock.return_value = 102
                return [{'entity_id': 'light.office', 'state': 'off'}]
            self.addon.get_ha_states.side_effect = states_after_deadline
            await self.addon.execute_command({'id': 'expired-snapshot', 'action': 'scene.create',
                'params': {'scene_id': 'snapshot', 'snapshot_entities': ['light.office']}, 'expires_at': 101})
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(self.addon.managed_scenes.active(), {})
        self.assertEqual(self.addon.command_journal.pending(), [('expired-snapshot', 'expired')])

    async def test_mocked_scene_crud_retains_edit_after_restart_and_delete_stays_deleted(self):
        async def command(command_id, action, params):
            await self.addon.execute_command({'id': command_id, 'action': action, 'params': params})
        entity = 'scene.lapahub_office'
        await command('create', 'scene.create', {'scene_id': entity, 'entities': self.definition})
        edited = {'light.office': {'state': 'on', 'brightness': 50}}
        await command('edit', 'scene.create', {'scene_id': entity, 'entities': edited})
        await command('run', 'scene.turn_on', {'entity_id': entity})
        self.assertEqual([call.args[:2] for call in self.addon.call_ha_service.await_args_list],
                         [('scene', 'create'), ('scene', 'create'), ('scene', 'create'), ('scene', 'turn_on')])
        self.addon.managed_scenes.close()
        self.addon.managed_scenes = ManagedScenes(self.path)
        self.addon.call_ha_service.reset_mock()
        await self.addon.restore_managed_scenes([{'entity_id': 'light.office'}])
        self.addon.call_ha_service.assert_awaited_once_with('scene', 'create', {
            'scene_id': 'lapahub_office', 'entities': edited})
        await command('delete', 'scene.delete', {'entity_id': entity})
        self.addon.managed_scenes.close()
        self.addon.managed_scenes = ManagedScenes(self.path)
        self.addon.call_ha_service.reset_mock()
        await self.addon.restore_managed_scenes([{'entity_id': 'light.office'}])
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(dict(self.addon.command_journal.pending()), {
            'create': 'service_accepted', 'edit': 'service_accepted',
            'run': 'service_accepted', 'delete': 'service_accepted'})
