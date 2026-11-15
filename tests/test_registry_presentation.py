"""Registry-derived wrapper evidence; synthetic HA/cloud only, no device actions."""
import ast
import asyncio
import copy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app'))
from registry_presentation import collect_registry_presentation, normalize_registry, unavailable_registry


def fixture():
    return [
        {'entity_id': 'fan.pump', 'id': 'wrapper-id', 'platform': 'switch_as_x',
         'options': {'switch_as_x': {'entity_id': 'switch.pump', 'invert': False},
                     'secret': 'not-exported'}, 'name': 'not-exported'},
        {'entity_id': 'switch.pump', 'id': 'source-id', 'platform': 'esphome',
         'device_id': 'not-exported', 'unique_id': 'not-exported'},
    ]


class NormalizationTests(unittest.TestCase):
    def test_exact_registry_link_is_allowlisted_without_customer_metadata(self):
        observed = normalize_registry(fixture())
        self.assertEqual(observed, {'complete': True, 'entries': [
            {'entityId': 'fan.pump', 'registryId': 'wrapper-id', 'wrapper': {
                'integration': 'switch_as_x', 'sourceEntityId': 'switch.pump',
                'sourceRegistryId': 'source-id', 'invert': False}},
            {'entityId': 'switch.pump', 'registryId': 'source-id'},
        ]})
        self.assertNotIn('not-exported', json.dumps(observed))

    def test_platform_and_registry_options_are_required_not_state_attributes(self):
        for platform in ('template', None, 'Switch_as_x'):
            entries = fixture()
            entries[0]['platform'] = platform
            entries[0]['attributes'] = {'switch_as_x': {'entity_id': 'switch.pump', 'invert': False}}
            self.assertEqual(normalize_registry(entries), {'complete': True, 'entries': []})

    def test_no_inversion_coercion_or_missing_options(self):
        for invert in (True, 0, None, 'false', [], {}):
            entries = fixture()
            entries[0]['options']['switch_as_x']['invert'] = invert
            self.assertEqual(normalize_registry(entries)['entries'], [])
        for options in (None, [], {}, {'switch_as_x': []}, {'switch_as_x': {'entity_id': 'switch.pump'}}):
            entries = fixture()
            entries[0]['options'] = options
            self.assertEqual(normalize_registry(entries)['entries'], [])

    def test_source_must_exist_and_be_exact_switch_domain(self):
        entries = fixture()
        self.assertEqual(normalize_registry(entries[:1])['entries'], [])
        for source in ('light.pump', 'switch.missing', 'source-id', 'switch.pump/extra', 'fan.pump'):
            entries = fixture()
            entries[0]['options']['switch_as_x']['entity_id'] = source
            self.assertEqual(normalize_registry(entries)['entries'], [])

    def test_duplicate_entity_or_registry_identity_invalidates_all_evidence(self):
        for mutate in (lambda e: e.update(id='different'), lambda e: e.update(entity_id='switch.other')):
            entries = fixture()
            duplicate = copy.deepcopy(entries[1])
            mutate(duplicate)
            entries.append(duplicate)
            self.assertEqual(normalize_registry(entries), {'complete': False, 'entries': []})

    def test_competing_aliases_never_choose_even_disabled_or_inverted(self):
        for variation in ('normal', 'inverted', 'disabled', 'malformed-invert'):
            entries = fixture()
            alias = copy.deepcopy(entries[0])
            alias.update(entity_id='light.pump', id='second-wrapper')
            if variation == 'inverted':
                alias['options']['switch_as_x']['invert'] = True
            if variation == 'disabled':
                alias['disabled_by'] = 'user'
            if variation == 'malformed-invert':
                del alias['options']['switch_as_x']['invert']
            entries.append(alias)
            self.assertEqual(normalize_registry(entries)['entries'], [])

    def test_source_chains_cycles_and_disabled_members_do_not_fold(self):
        entries = fixture()
        entries[1].update(platform='switch_as_x', options={'switch_as_x': {'entity_id': 'switch.other', 'invert': False}})
        entries.append({'id': 'last-source', 'entity_id': 'switch.other', 'platform': 'esphome'})
        # Even the second link cannot fold: its wrapper is used as another source.
        self.assertEqual(normalize_registry(entries)['entries'], [])
        entries[1]['options']['switch_as_x']['entity_id'] = 'fan.pump'
        self.assertEqual(normalize_registry(entries)['entries'], [])
        for index in (0, 1):
            entries = fixture()
            entries[index]['disabled_by'] = 'integration'
            self.assertEqual(normalize_registry(entries)['entries'], [])

    def test_renames_follow_exact_ids_not_old_names_or_suffixes(self):
        entries = fixture()
        entries[1]['entity_id'] = 'switch.renamed'
        self.assertEqual(normalize_registry(entries)['entries'], [])
        entries[0]['options']['switch_as_x']['entity_id'] = 'switch.renamed'
        link = normalize_registry(entries)['entries'][0]['wrapper']
        self.assertEqual(link['sourceEntityId'], 'switch.renamed')
        self.assertEqual(link['sourceRegistryId'], 'source-id')

    def test_malformed_or_oversized_snapshot_is_explicitly_unavailable(self):
        for entries in (None, {}, 'secret', [None], [{}], fixture() + [{'id': 'x', 'entity_id': 'bad'}]):
            self.assertEqual(normalize_registry(entries), {'complete': False, 'entries': []})
        with patch('registry_presentation.MAX_REGISTRY_ENTRIES', 1):
            self.assertFalse(normalize_registry(fixture())['complete'])
        with patch('registry_presentation.MAX_PRESENTATION_ENTRIES', 1):
            self.assertFalse(normalize_registry(fixture())['complete'])

    def test_complete_empty_registry_removes_previous_links(self):
        self.assertEqual(normalize_registry([]), {'complete': True, 'entries': []})


def actual_type(base):
    source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
    cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'LapaHubAddon')
    methods = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {'push_devices_to_cloud', 'sync_devices'}]
    ns = dict(datetime=datetime, timezone=timezone, aiohttp=aiohttp,
              logger=logging.getLogger('registry-transport'), HA_BASE_URL=base,
              SUPERVISOR_TOKEN='synthetic-ha', collect_registry_presentation=collect_registry_presentation,
              unavailable_registry=unavailable_registry)
    for method in methods:
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), ns)
    return type('ActualRegistryTransport', (), {method.name: ns[method.name] for method in methods})


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = fixture()
        self.mode = 'success'
        self.cloud_status = 200
        self.ws_calls = []
        self.cloud_calls = []
        self.before_result = None
        self.read_entered = asyncio.Event()
        self.read_gate = None

        async def websocket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({'type': 'auth_required'})
            auth = await ws.receive_json()
            self.assertEqual(auth, {'type': 'auth', 'access_token': 'synthetic-ha'})
            if self.mode == 'auth-invalid':
                await ws.send_json({'type': 'auth_invalid'})
                await ws.close()
                return ws
            await ws.send_json({'type': 'auth_ok'})
            command = await ws.receive_json()
            self.ws_calls.append(command)
            self.assertEqual(command, {'id': 1, 'type': 'config/entity_registry/list'})
            self.read_entered.set()
            if self.read_gate:
                await self.read_gate.wait()
            if self.before_result:
                self.before_result()
            response = {'id': 1, 'type': 'result', 'success': True, 'result': self.registry}
            if self.mode == 'failed':
                response = {'id': 1, 'type': 'result', 'success': False, 'error': {'message': 'secret'}}
            elif self.mode == 'wrong-id':
                response['id'] = 2
            elif self.mode == 'partial':
                del response['result']
            elif self.mode == 'disconnect':
                await ws.close()
                return ws
            try:
                await ws.send_json(response)
            except ConnectionResetError:
                pass
            await ws.close()
            return ws

        async def cloud(request):
            self.cloud_calls.append((request.headers['Authorization'], await request.json()))
            return web.json_response({}, status=self.cloud_status)

        app = web.Application()
        app.router.add_get('/api/websocket', websocket)
        app.router.add_post('/cloud/syncDevices', cloud)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        self.session = aiohttp.ClientSession(trust_env=False)

        class Gateway:
            def ws_connect(inner, url, **kwargs):
                return self.session.ws_connect(url, **kwargs)
            def post(inner, url, **kwargs):
                self.assertTrue(url.endswith('/syncDevices'))
                return self.session.post(self.base + '/cloud/syncDevices', **kwargs)

        self.addon = actual_type(self.base)()
        self.addon.session = Gateway()
        self.addon.firebase_credentials = {'token': 'synthetic-cloud'}
        self.addon.firebase_project = 'synthetic'
        self.addon.hub_id = 'synthetic'
        self.devices = [{'entity_id': 'fan.pump', 'state': 'off', 'last_updated': '2026-11-15T00:00:00Z',
                         'attributes': {'friendly_name': 'Pump'}}]

    async def asyncTearDown(self):
        if self.read_gate:
            self.read_gate.set()
        await self.session.close()
        await self.runner.cleanup()

    async def test_actual_push_keeps_state_and_includes_fresh_allowlisted_link(self):
        await self.addon.push_devices_to_cloud(self.devices)
        auth, body = self.cloud_calls[0]
        self.assertEqual(auth, 'Bearer synthetic-cloud')
        self.assertEqual(body['devices'], self.devices)
        self.assertEqual(body['presentationRegistryV1'], dict(normalize_registry(fixture()), sequence=1))
        self.assertNotIn('not-exported', json.dumps(body))
        self.assertEqual(len(self.ws_calls), 1)

    async def test_success_failure_recovery_never_reuses_cached_link(self):
        await self.addon.push_devices_to_cloud(self.devices)
        for mode in ('auth-invalid', 'failed', 'wrong-id', 'partial', 'disconnect'):
            self.mode = mode
            await self.addon.push_devices_to_cloud(self.devices)
            self.assertEqual(self.cloud_calls[-1][1]['presentationRegistryV1'], {
                'complete': False, 'entries': [], 'sequence': len(self.cloud_calls)})
        self.mode = 'success'
        self.registry = []
        await self.addon.push_devices_to_cloud(self.devices)
        self.assertEqual(self.cloud_calls[-1][1]['presentationRegistryV1'], {
            'complete': True, 'entries': [], 'sequence': 7})
        self.registry = fixture()
        await self.addon.push_devices_to_cloud(self.devices)
        self.assertEqual(self.cloud_calls[-1][1]['presentationRegistryV1']['sequence'], 8)
        self.assertEqual(len(self.cloud_calls[-1][1]['presentationRegistryV1']['entries']), 2)

    async def test_failed_cloud_write_next_attempt_uses_new_observation_and_sequence(self):
        self.cloud_status = 503
        await self.addon.push_devices_to_cloud(self.devices)
        self.cloud_status = 200
        self.registry = []
        await self.addon.push_devices_to_cloud(self.devices)
        self.assertEqual([b['presentationRegistryV1']['sequence'] for _, b in self.cloud_calls], [1, 2])
        self.assertEqual(self.cloud_calls[-1][1]['presentationRegistryV1']['entries'], [])
        self.assertEqual(len(self.ws_calls), 2)

    async def test_refresh_during_read_keeps_captured_token_and_next_session_restarts_sequence(self):
        await self.addon.push_devices_to_cloud(self.devices)
        self.before_result = lambda: setattr(self.addon, 'firebase_credentials', {'token': 'new-cloud-session'})
        await self.addon.push_devices_to_cloud(self.devices)
        self.assertEqual(self.cloud_calls[-1][0], 'Bearer synthetic-cloud')
        self.assertEqual(self.cloud_calls[-1][1]['presentationRegistryV1']['sequence'], 2)
        self.before_result = None
        await self.addon.push_devices_to_cloud(self.devices)
        self.assertEqual(self.cloud_calls[-1][0], 'Bearer new-cloud-session')
        self.assertEqual(self.cloud_calls[-1][1]['presentationRegistryV1']['sequence'], 1)

    async def test_timeout_posts_explicit_unavailable_without_unbounded_wait(self):
        self.read_gate = asyncio.Event()
        with patch('registry_presentation.REGISTRY_READ_TIMEOUT', .02):
            await self.addon.push_devices_to_cloud(self.devices)
        self.assertEqual(self.cloud_calls[0][1]['presentationRegistryV1'], {
            'complete': False, 'entries': [], 'sequence': 1})

    async def test_shutdown_cancellation_propagates_without_cloud_post(self):
        self.read_gate = asyncio.Event()
        task = asyncio.create_task(self.addon.push_devices_to_cloud(self.devices))
        await self.read_entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.cloud_calls, [])

    async def test_missing_credentials_never_reads_ha_or_posts(self):
        self.addon.firebase_credentials = None
        await self.addon.push_devices_to_cloud(self.devices)
        self.assertEqual(self.ws_calls, [])
        self.assertEqual(self.cloud_calls, [])

    async def test_state_read_outage_explicitly_invalidates_without_registry_read_or_state_replay(self):
        await self.addon.push_devices_to_cloud(self.devices)
        self.addon.get_ha_states = AsyncMock(return_value=[])
        await self.addon.sync_devices()
        self.assertEqual(len(self.ws_calls), 1)
        auth, body = self.cloud_calls[-1]
        self.assertEqual(auth, 'Bearer synthetic-cloud')
        self.assertIs(body['presentationOnly'], True)
        self.assertEqual(body['devices'], [])
        self.assertEqual(body['presentationRegistryV1'], {'complete': False, 'entries': [], 'sequence': 2})
        self.assertNotIn('presentationOnly', self.cloud_calls[0][1])
        await self.addon.push_devices_to_cloud(self.devices)
        self.assertEqual(len(self.ws_calls), 2)
        self.assertEqual(self.cloud_calls[-1][1]['presentationRegistryV1']['sequence'], 3)
        self.assertTrue(self.cloud_calls[-1][1]['presentationRegistryV1']['complete'])
