"""Camera discovery/realtime must never forward HA bearer/query camera secrets."""
import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app'))


def actual_type():
    source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
    cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'LapaHubAddon')
    names = {'sync_devices', 'handle_state_change'}
    ns = {'logger': logging.getLogger('camera-test')}
    try:
        from camera_capture import safe_camera_attributes, safe_camera_state
        ns.update(safe_camera_attributes=safe_camera_attributes, safe_camera_state=safe_camera_state)
    except ImportError:
        pass
    for method in cls.body:
        if isinstance(method, ast.AsyncFunctionDef) and method.name in names:
            exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), ns)
    return type('ActualCameraDiscoveryMethods', (), {name: ns[name] for name in names})


class CameraPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setup_addon(self):
        addon = actual_type()()
        addon.devices = {}
        addon.entity_device_map = {}
        addon.physical_devices = {}
        addon.firebase_credentials = {'token': 'cloud-fixture'}
        addon.sensor_realtime = False
        addon.state_publisher = SimpleNamespace(offer=Mock(), dropped=0)
        addon.push_devices_to_cloud = AsyncMock()
        return addon

    def state(self):
        return {'entity_id': 'camera.fixture', 'state': 'streaming', 'attributes': {
            'friendly_name': 'Fixture', 'supported_features': 3, 'motion_detection': True,
            'access_token': 'forbidden-secret', 'entity_picture': '/api/camera_proxy/fixture?token=forbidden-secret',
            'stream_source': 'rtsp://user:forbidden-secret@host', 'nested': {'credential': 'forbidden-secret'}},
            'last_updated': '2026-10-29T00:00:00Z'}

    def assert_safe(self, device):
        self.assertNotIn('forbidden-secret', json.dumps(device))
        self.assertEqual(device['attributes'], {'friendly_name': 'Fixture', 'supported_features': 3, 'motion_detection': True})

    async def test_actual_discovery_adds_only_safe_camera_metadata(self):
        addon = self.setup_addon()
        addon.get_ha_states = AsyncMock(return_value=[self.state()])
        await addon.sync_devices()
        devices = addon.push_devices_to_cloud.await_args.args[0]
        self.assertEqual(len(devices), 1)
        self.assert_safe(devices[0])
        self.assert_safe(addon.devices['camera.fixture'])

    async def test_actual_realtime_caches_and_publishes_only_safe_camera_metadata(self):
        addon = self.setup_addon()
        await addon.handle_state_change({'entity_id': 'camera.fixture', 'new_state': self.state()})
        addon.state_publisher.offer.assert_called_once()
        self.assert_safe(addon.state_publisher.offer.call_args.args[0])
        self.assert_safe(addon.devices['camera.fixture'])

    async def test_invalid_attribute_types_and_secret_state_are_not_forwarded(self):
        from camera_capture import safe_camera_attributes, safe_camera_state
        self.assertEqual(safe_camera_attributes({'friendly_name': {'token': 'secret'}, 'supported_features': True,
                                                  'motion_detection': 'secret', 'entity_picture': 'secret'}), {})
        self.assertEqual(safe_camera_attributes(None), {})
        self.assertEqual(safe_camera_attributes({'friendly_name': 'x' * 300})['friendly_name'], 'x' * 200)
        self.assertEqual(safe_camera_state('rtsp://user:secret@host'), 'unknown')
        addon = self.setup_addon()
        state = self.state()
        state['state'] = 'rtsp://user:forbidden-secret@host'
        await addon.handle_state_change({'entity_id': 'camera.fixture', 'new_state': state})
        published = addon.state_publisher.offer.call_args.args[0]
        self.assertEqual(published['state'], 'unknown')
        self.assert_safe(published)

    async def test_non_camera_control_attributes_are_preserved(self):
        addon = self.setup_addon()
        state = {'entity_id': 'climate.fixture', 'state': 'heat', 'attributes': {'temperature': 21}}
        await addon.handle_state_change({'entity_id': 'climate.fixture', 'new_state': state})
        self.assertEqual(addon.state_publisher.offer.call_args.args[0]['attributes'], {'temperature': 21})
