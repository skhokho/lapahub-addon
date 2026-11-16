import ast
import asyncio
import logging
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app'))
from energy_payload import EnergyDeliveryError, collect_energy_payload

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def sensor(value, unit='W', kind='power', name='sensor.power'):
    return {'entity_id': name, 'state': value, 'last_reported': NOW.isoformat(), 'last_updated': '2020-01-01T00:00:00+00:00',
            'attributes': {'device_class': kind, 'unit_of_measurement': unit}}


def actual_methods():
    source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
    addon = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'LapaHubAddon')
    namespace = {'asyncio': asyncio, 'datetime': datetime, 'timezone': timezone,
                 'timedelta': timedelta, 'collect_energy_payload': collect_energy_payload,
                 'EnergyDeliveryError': EnergyDeliveryError,
                 'logger': logging.getLogger('energy-test'), 'aiohttp': __import__('aiohttp')}
    methods = [n for n in addon.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {'report_energy', 'push_energy_to_cloud'}]
    for method in methods:
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), namespace)
    return type('EnergyMethods', (), {m.name: namespace[m.name] for m in methods})


class PayloadTests(unittest.TestCase):
    def test_zero_preserved_and_unknown_nonfinite_never_become_zero(self):
        for value in ('unavailable', 'unknown', '', None, True, 'NaN', 'Infinity', '-Infinity'):
            with self.subTest(value=value):
                report = collect_energy_payload([sensor(value)], {}, NOW)
                self.assertIsNone(report['sensors']['sensor.power']['value'])
        report = collect_energy_payload([sensor('0')], {}, NOW)
        entry = report['sensors']['sensor.power']
        self.assertEqual(entry['value'], 0)
        self.assertEqual(entry['observed_at'], NOW.isoformat())
        self.assertEqual(entry['state_updated_at'], '2020-01-01T00:00:00+00:00')
        self.assertEqual(report['timestamp'], NOW.isoformat())
        self.assertEqual(report['energy_payload_version'], 2)

    def test_source_report_freshness_masks_all_power_paths_without_masking_counters(self):
        # Reproduces the actual redacted source clock: zero last reported around
        # 00:08, fetched after 01:00. This does not prove the physical zero wrong.
        fetched = datetime(2026, 10, 29, 1, 0, 25, tzinfo=timezone.utc)
        stale = sensor('0')
        stale['last_reported'] = '2026-10-29T00:08:14.824798Z'
        prefs = {'energy_sources': [{'type': 'grid', 'power': [{'stat_rate': 'sensor.power'}]}]}
        report = collect_energy_payload([stale], prefs, fetched)
        self.assertIsNone(report['sensors']['sensor.power']['value'])
        self.assertIsNone(report['dashboard_sources']['sensor.power']['value'])
        # Fresh unchanged reports remain measured, regardless of last_updated.
        stale['last_reported'] = (fetched - timedelta(seconds=30)).isoformat()
        report = collect_energy_payload([stale], prefs, fetched)
        self.assertEqual(report['sensors']['sensor.power']['value'], 0)
        self.assertEqual(report['sensors']['sensor.power']['observed_at'], stale['last_reported'])
        self.assertEqual(report['dashboard_sources']['sensor.power']['value'], 0)
        self.assertEqual(report['dashboard_sources']['sensor.power']['observed_at'], stale['last_reported'])
        # Direct dashboard power is also gated without device_class metadata.
        stale['attributes'].pop('device_class')
        stale.pop('last_reported')
        self.assertIsNone(collect_energy_payload([stale], prefs, fetched)['dashboard_sources']['sensor.power']['value'])
        # State-of-charge and cumulative readings retain snapshot semantics.
        soc = sensor('55', '%', 'battery', 'sensor.soc')
        counter = sensor('123', 'kWh', 'energy', 'sensor.counter')
        for item in (soc, counter):
            item.pop('last_reported')
        prefs['energy_sources'][0]['flow_from'] = [{'stat_energy_from': 'sensor.counter'}]
        report = collect_energy_payload([soc, counter], prefs, fetched)
        self.assertEqual(report['sensors']['sensor.soc']['value'], 55)
        self.assertEqual(report['dashboard_sources']['sensor.counter']['value'], 123)

    def test_stale_power_cannot_return_through_legacy_dashboard_fields(self):
        state = sensor('0')
        state.pop('last_reported')
        cases = [
            {'energy_sources': [{'type': 'grid', 'power': 'sensor.power'}]},
            {'energy_sources': [{'type': 'grid', 'power': [{'stat_rate': 'sensor.power'}]}]},
            {'energy_sources': [{'type': 'grid', 'flow_from': [{'stat_energy_from': 'sensor.power'}]}]},
            {'energy_sources': [{'type': 'grid', 'flow_to': [{'stat_energy_to': 'sensor.power'}]}]},
            {'energy_sources': [{'type': 'solar', 'stat_energy_from': 'sensor.power'}]},
            {'energy_sources': [{'type': 'battery', 'entity_id': 'sensor.power'}]},
            {'device_consumption': [{'stat_consumption': 'sensor.power'}]},
        ]
        for prefs in cases:
            with self.subTest(prefs=prefs):
                report = collect_energy_payload([state], prefs, NOW)
                self.assertIsNone(report['sensors']['sensor.power']['value'])
                self.assertIsNone(report['dashboard_sources']['sensor.power']['value'])
                self.assertTrue(report['dashboard_sources']['sensor.power']['is_power_sensor'])

    def test_actual_direct_stat_rate_shape_preserves_missing_configured_members(self):
        # Redacted actual preference shape: grid, battery and solar expose
        # stat_rate directly, not only the legacy power list. IDs are fixtures.
        for kind in ('grid', 'battery', 'solar'):
            with self.subTest(kind=kind):
                prefs = {'energy_sources': [{'type': kind, 'stat_rate': 'sensor.power'}]}
                report = collect_energy_payload([], prefs, NOW)
                entry = report['dashboard_sources']['sensor.power']
                self.assertEqual(entry['type'], kind)
                self.assertTrue(entry['is_power_sensor'])
                self.assertIsNone(entry['value'])
                self.assertNotIn('unit', entry)  # Never guess units for absent HA state.
                report = collect_energy_payload([sensor('0')], prefs, NOW)
                self.assertEqual(report['dashboard_sources']['sensor.power']['value'], 0)
                state = sensor('0')
                state['last_reported'] = (NOW - timedelta(minutes=50)).isoformat()
                entry = collect_energy_payload([state], prefs, NOW)['dashboard_sources']['sensor.power']
                self.assertIsNone(entry['value'])
                self.assertEqual(entry['unit'], 'W')

    def test_source_report_timestamp_bounds_and_timezone(self):
        for age, expected in ((600, 0), (600.001, None), (-60, 0), (-60.001, None)):
            with self.subTest(age=age):
                state = sensor('0')
                state['last_reported'] = (NOW - timedelta(seconds=age)).isoformat()
                self.assertEqual(collect_energy_payload([state], {}, NOW)['sensors']['sensor.power']['value'], expected)
        for stamp in (None, '', 'invalid', '2026-10-28T00:00:00', True, 42):
            with self.subTest(stamp=stamp):
                state = sensor('0')
                state['last_reported'] = stamp
                self.assertIsNone(collect_energy_payload([state], {}, NOW)['sensors']['sensor.power']['value'])
        state = sensor('0')
        state['last_reported'] = '2026-10-28T02:00:00+02:00'
        self.assertEqual(collect_energy_payload([state], {}, NOW)['sensors']['sensor.power']['value'], 0)

    def test_supported_units_explicit_and_no_missing_power_synthesized(self):
        for unit in ('W', 'kW', 'MW'):
            self.assertEqual(collect_energy_payload([sensor('-1.5', unit)], {}, NOW)['sensors']['sensor.power']['value'], -1.5)
        for unit in ('kWh', None, '', 'volts'):
            self.assertEqual(collect_energy_payload([sensor(7, unit)], {}, NOW)['sensors'], {})
        self.assertEqual(collect_energy_payload([sensor(0, '%', 'battery')], {}, NOW)['sensors']['sensor.power']['value'], 0)
        self.assertIsNone(collect_energy_payload([sensor(101, '%', 'battery')], {}, NOW)['sensors']['sensor.power']['value'])

    def test_cumulative_dashboard_metadata_not_power(self):
        states = [sensor('123', 'kWh', 'energy', 'sensor.import'), sensor('0')]
        prefs = {'energy_sources': [{'type': 'grid', 'power': [{'stat_rate': 'sensor.power'}],
                                    'flow_from': [{'stat_energy_from': 'sensor.import'}]}]}
        report = collect_energy_payload(states, prefs, NOW)
        self.assertFalse(report['dashboard_sources']['sensor.import']['is_power_sensor'])
        self.assertTrue(report['dashboard_sources']['sensor.power']['is_power_sensor'])
        self.assertNotIn('sensor.import', report['sensors'])

    def test_limit_and_timezone_are_not_silently_truncated_or_assumed(self):
        with self.assertRaises(ValueError):
            collect_energy_payload([sensor(1, name=f'sensor.p{i}') for i in range(513)], {}, NOW)
        with self.assertRaises(ValueError):
            collect_energy_payload([], {}, datetime(2026, 10, 28))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def addon(self):
        addon = actual_methods()()
        addon.energy_prefs_last_fetch = datetime.now(timezone.utc)
        addon.energy_prefs = {}
        addon.get_ha_states = AsyncMock(return_value=[sensor('0')])
        addon.push_energy_to_cloud = AsyncMock(side_effect=[RuntimeError('HTTP503'), None])
        addon.last_energy_report = None
        return addon

    async def test_retry_same_observation_not_new_timestamp_or_fake_success(self):
        addon = self.addon()
        with self.assertRaises(RuntimeError):
            await addon.report_energy()
        self.assertIsNone(addon.last_energy_report)
        pending = addon.pending_energy_report
        self.assertEqual(pending['energy_payload_version'], 2)
        await addon.report_energy()
        addon.get_ha_states.assert_awaited_once()
        self.assertIs(addon.push_energy_to_cloud.await_args_list[0].args[0], pending)
        self.assertIs(addon.push_energy_to_cloud.await_args_list[1].args[0], pending)
        self.assertEqual(addon.last_energy_report.isoformat(), pending['timestamp'])
        self.assertIsNone(addon.pending_energy_report)
        self.assertEqual(addon.push_energy_to_cloud.await_args_list[1].args[0]['energy_payload_version'], 2)

    async def test_permanent_rejection_discards_but_transient_retains(self):
        for status in (400, 401, 403, 409, 422, 408, 429, 503):
            addon = self.addon()
            addon.push_energy_to_cloud = AsyncMock(side_effect=EnergyDeliveryError(status))
            with self.assertRaises(EnergyDeliveryError):
                await addon.report_energy()
            self.assertIsNone(addon.last_energy_report)
            if status in (408, 429, 503):
                self.assertIsNotNone(addon.pending_energy_report)
            else:
                self.assertIsNone(addon.pending_energy_report)

    async def test_http_rejection_and_transport_error_propagate(self):
        class Response:
            status = 503
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        class Session:
            def post(self, *args, **kwargs):
                return Response()
        addon = actual_methods()()
        addon.firebase_credentials = {'token': 'test-only'}
        addon.firebase_project = 'demo-lapahub'
        addon.hub_id = 'test-hub'
        addon.session = Session()
        with self.assertRaisesRegex(RuntimeError, 'HTTP 503'):
            await addon.push_energy_to_cloud({'timestamp': NOW.isoformat()})
        class BrokenSession:
            def post(self, *args, **kwargs):
                raise OSError('connection lost')
        addon.session = BrokenSession()
        with self.assertRaises(OSError):
            await addon.push_energy_to_cloud({'timestamp': NOW.isoformat()})

    async def test_failed_ha_fetch_never_reports_success(self):
        addon = self.addon()
        addon.get_ha_states.return_value = []
        with self.assertRaises(RuntimeError):
            await addon.report_energy()
        addon.push_energy_to_cloud.assert_not_awaited()
        self.assertIsNone(addon.last_energy_report)

    async def test_missing_credentials_never_silently_succeeds(self):
        addon = actual_methods()()
        addon.firebase_credentials = {}
        with self.assertRaises(RuntimeError):
            await addon.push_energy_to_cloud({'timestamp': NOW.isoformat()})
