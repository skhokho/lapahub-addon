import ast
import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app'))
from managed_scenes import ManagedScenes


def actual_scene_type():
    source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
    addon = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.ClassDef) and node.name == 'LapaHubAddon')
    methods = [node for node in addon.body if isinstance(node, ast.AsyncFunctionDef) and node.name in {'create_scene', 'delete_scene', 'trigger_scene', 'restore_managed_scenes'}]
    namespace = {'asyncio': asyncio, 'logger': logging.getLogger('scene-test')}
    for method in methods:
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), namespace)
    return type('ActualSceneMethods', (), {method.name: namespace[method.name] for method in methods})


class ManagedSceneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'scenes.sqlite'
        self.addon = actual_scene_type()()
        self.addon.managed_scenes = ManagedScenes(self.path)
        self.addon.scene_lock = asyncio.Lock()
        self.addon.call_ha_service = AsyncMock(return_value=True)
        self.addon.get_ha_states = AsyncMock(return_value=[{'entity_id': 'light.office', 'state': 'off'}])
        self.addon.log_activity = lambda *args: None
        self.definition = {'light.office': {'state': 'off', 'brightness': 100}}
    async def asyncTearDown(self):
        self.addon.managed_scenes.close()
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
