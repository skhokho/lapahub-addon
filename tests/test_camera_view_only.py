"""Camera viewing must never enable controls, scene snapshots or activations."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import unittest
import test_managed_scenes as scene_tests


class CameraSceneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await scene_tests.ManagedSceneTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await scene_tests.ManagedSceneTests.asyncTearDown(self)

    async def test_mixed_snapshot_rejected_before_ha_lookup_or_copy(self):
        self.addon.get_ha_states = AsyncMock(side_effect=AssertionError('Snapshot lookup forbidden'))
        with self.assertRaises(ValueError):
            await self.addon.create_scene('mixed', snapshot_entities=['light.office', 'camera.fixture'])
        self.addon.get_ha_states.assert_not_awaited()
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(self.addon.managed_scenes.active(), {})

    async def test_mixed_explicit_update_rejected_atomically(self):
        await self.addon.create_scene('office', entities=self.definition)
        self.addon.call_ha_service.reset_mock()
        with self.assertRaises(ValueError):
            await self.addon.create_scene('office', entities={**self.definition, 'camera.fixture': {
                'state': 'streaming', 'access_token': 'forbidden-camera-secret',
                'entity_picture': '/private?token=forbidden-camera-secret'}})
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(self.addon.managed_scenes.active()['scene.lapahub_office']['entities'], self.definition)
        rows = self.addon.managed_scenes.db.execute('SELECT definition FROM scenes').fetchall()
        self.assertNotIn('forbidden-camera-secret', str(rows))

    async def test_camera_in_ignored_snapshot_argument_is_still_rejected(self):
        with self.assertRaises(ValueError):
            await self.addon.create_scene('mixed', entities=self.definition, snapshot_entities=['camera.fixture'])
        self.addon.call_ha_service.assert_not_awaited()

    async def test_legacy_camera_definition_is_never_restored_or_activated(self):
        definition = {'scene_id': 'lapahub_legacy', 'entities': {'camera.fixture': {'state': 'streaming'}}}
        self.addon.managed_scenes.save('scene.lapahub_legacy', definition)
        before = self.addon.managed_scenes.active()
        self.assertFalse(await self.addon.restore_managed_scenes([]))
        with self.assertRaises(ValueError):
            await self.addon.trigger_scene('scene.lapahub_legacy')
        self.addon.call_ha_service.assert_not_awaited()
        self.addon.get_ha_states.assert_not_awaited()
        self.assertEqual(self.addon.managed_scenes.active(), before)

    async def test_imported_scene_with_camera_member_is_rejected(self):
        self.addon.get_ha_states = AsyncMock(return_value=[{'entity_id': 'scene.imported', 'attributes': {
            'entity_id': ['light.office', 'camera.fixture']}}])
        with self.assertRaises(ValueError):
            await self.addon.trigger_scene('scene.imported')
        self.addon.call_ha_service.assert_not_awaited()

    async def test_imported_unknown_members_fail_closed_but_known_light_runs(self):
        self.addon.get_ha_states = AsyncMock(return_value=[{'entity_id': 'scene.imported', 'attributes': {}}])
        self.assertFalse(await self.addon.trigger_scene('scene.imported'))
        self.addon.call_ha_service.assert_not_awaited()
        self.addon.get_ha_states.return_value = [{'entity_id': 'scene.imported', 'attributes': {'entity_id': ['light.office']}}]
        self.assertTrue(await self.addon.trigger_scene('scene.imported'))
        self.addon.call_ha_service.assert_awaited_once_with('scene', 'turn_on', {'entity_id': 'scene.imported'})

    async def test_actual_command_handler_blocks_camera_controls_and_qualified_services(self):
        for index, (entity, action) in enumerate((('camera.fixture', 'turn_on'), ('camera.fixture', 'turn_off'),
                ('camera.fixture', 'camera.record'), ('light.office', 'camera.enable_motion_detection'))):
            with self.subTest(entity=entity, action=action):
                await self.addon.execute_command({'id': 'camera-deny-' + str(index), 'entity_id': entity,
                                                  'action': action, 'params': {}})
        self.addon.call_ha_service.assert_not_awaited()
        self.assertTrue(all(outcome != 'service_accepted' for _, outcome in self.addon.command_journal.pending()))

    async def test_generic_scene_turn_on_cannot_bypass_member_check(self):
        self.addon.get_ha_states = AsyncMock(return_value=[{'entity_id': 'scene.imported', 'attributes': {
            'entity_id': ['light.office', 'camera.fixture']}}])
        await self.addon.execute_command({'id': 'scene-camera-deny', 'entity_id': 'scene.imported',
                                          'action': 'turn_on', 'params': {}})
        self.addon.call_ha_service.assert_not_awaited()
        self.assertNotIn(('scene-camera-deny', 'service_accepted'), self.addon.command_journal.pending())

    async def test_low_level_camera_service_is_denied_before_http(self):
        source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
        cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'LapaHubAddon')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'call_ha_service')
        ns = {'logger': logging.getLogger('view-only-test'), 'SUPERVISOR_TOKEN': 'fixture',
              'HA_BASE_URL': 'http://invalid', 'aiohttp': SimpleNamespace(ClientTimeout=lambda **kw: None)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), ns)
        self.addon.session = SimpleNamespace(post=Mock(side_effect=AssertionError('No camera control HTTP')))
        self.assertFalse(await ns['call_ha_service'](self.addon, 'camera', 'turn_on', {'entity_id': 'camera.fixture'}))
        self.assertFalse(await ns['call_ha_service'](self.addon, 'homeassistant', 'turn_on',
            {'entity_id': ['light.office', 'camera.fixture']}))
        self.assertFalse(await ns['call_ha_service'](self.addon, 'scene', 'create',
            {'entities': {'light.office': {'state': 'off'}, 'camera.fixture': {'state': 'streaming'}}}))
        self.addon.session.post.assert_not_called()

    async def test_resolved_camera_typed_alias_is_rejected_before_attribute_copy(self):
        self.addon.get_ha_states = AsyncMock(return_value=[{'entity_id': 'light.alias', 'domain': 'camera',
            'state': 'streaming', 'attributes': {'access_token': 'forbidden-camera-secret'}}])
        with self.assertRaises(ValueError):
            await self.addon.create_scene('alias', snapshot_entities=['light.alias'])
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(self.addon.managed_scenes.active(), {})

    async def test_create_and_update_legacy_command_payloads_cannot_persist_cameras(self):
        for action in ('scene.create', 'scene.update'):
            await self.addon.execute_command({'id': action + '-deny', 'action': action, 'params': {
                'scene_id': 'mixed', 'snapshot_entities': ['light.office', 'camera.fixture']}})
        self.addon.get_ha_states.assert_not_awaited()
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(self.addon.managed_scenes.active(), {})
        self.assertTrue(all(outcome != 'service_accepted' for _, outcome in self.addon.command_journal.pending()))
