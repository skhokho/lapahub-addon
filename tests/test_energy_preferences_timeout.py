"""Bound the actual metadata websocket; no HA instance or device operation."""
import ast
import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import unittest


class Socket:
    def __init__(self, stop):
        self.stop, self.reads = stop, 0
        self.send_json = AsyncMock()
        self.close = AsyncMock()
    async def __aenter__(self):
        if self.stop == -1: await asyncio.Event().wait()
        return self
    async def __aexit__(self, *args): await self.close()
    async def receive_json(self):
        self.reads += 1
        if self.reads == self.stop: await asyncio.Event().wait()
        return ({'type': 'auth_required'}, {'type': 'auth_ok'},
                {'success': True, 'result': {'energy_sources': []}})[self.reads - 1]


def actual_type(budgets):
    source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
    cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'LapaHubAddon')
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'fetch_energy_prefs')
    def timeout(seconds):
        budgets.append(seconds)
        return asyncio.timeout(.01)
    ns = {'logger': logging.getLogger('prefs-test'), 'HA_BASE_URL': 'http://synthetic',
          'SUPERVISOR_TOKEN': 'synthetic-token', 'datetime': datetime, 'timezone': timezone,
          'asyncio': SimpleNamespace(timeout=timeout)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), ns)
    return type('ActualPreferencesMethod', (), {'fetch_energy_prefs': ns['fetch_energy_prefs']})


class PreferenceTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setup_addon(self, stop):
        self.budgets = []
        addon = actual_type(self.budgets)()
        addon.energy_prefs = {'energy_sources': [{'type': 'grid'}]}
        addon.energy_prefs_last_fetch = datetime(2026, 1, 1, tzinfo=timezone.utc)
        addon.log_activity = Mock()
        socket = Socket(stop)
        addon.session = SimpleNamespace(ws_connect=Mock(return_value=socket))
        return addon, socket

    async def test_handshake_greeting_auth_and_result_stalls_are_bounded(self):
        for stop in (-1, 1, 2, 3):
            with self.subTest(stage=stop):
                addon, socket = self.setup_addon(stop)
                cached, fetched = addon.energy_prefs, addon.energy_prefs_last_fetch
                self.assertFalse(await asyncio.wait_for(addon.fetch_energy_prefs(), .1))
                self.assertIs(addon.energy_prefs, cached)
                self.assertEqual(addon.energy_prefs_last_fetch, fetched)
                self.assertEqual(self.budgets, [10])
                if stop != -1: socket.close.assert_awaited_once()

    async def test_success_updates_cache_without_new_device_calls(self):
        addon, socket = self.setup_addon(0)
        self.assertTrue(await addon.fetch_energy_prefs())
        self.assertEqual(addon.energy_prefs, {'energy_sources': []})
        self.assertGreater(addon.energy_prefs_last_fetch.year, 2025)
        socket.close.assert_awaited_once()

    async def test_shutdown_cancellation_propagates(self):
        addon, socket = self.setup_addon(1)
        task = asyncio.create_task(addon.fetch_energy_prefs())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        socket.close.assert_awaited_once()
