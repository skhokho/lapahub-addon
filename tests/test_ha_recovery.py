"""Actual socket/recovery methods with no HA, physical devices, or real sleeps."""
import ast
import asyncio
import json
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


def actual_type(asyncio_api=asyncio):
    source=Path(__file__).resolve().parents[1]/'lapahub/rootfs/app/run.py'
    addon=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='LapaHubAddon')
    names={'ha_websocket_loop','connect_ha_websocket','handle_ws_message','refresh_automation_bindings','enrich_automation_configs','automation_refresh_loop'}
    methods=[n for n in addon.body if isinstance(n,ast.AsyncFunctionDef) and n.name in names]
    ns={'asyncio':asyncio_api,'logger':logging.getLogger('ha-recovery-test'),'json':json,'HA_BASE_URL':'http://supervisor/core','SUPERVISOR_TOKEN':'test-token','aiohttp':SimpleNamespace(WSMsgType=SimpleNamespace(TEXT=1,CLOSED=2,ERROR=3))}
    for method in methods:exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),str(source),'exec'),ns)
    return type('ActualRecoveryMethods',(),{m.name:ns[m.name] for m in methods})


class EmptySocket:
    closed=False
    def __init__(self,messages):self.receive_json=AsyncMock(side_effect=messages);self.send_json=AsyncMock();self.close=AsyncMock()
    def __aiter__(self):return self
    async def __anext__(self):raise StopAsyncIteration


class HaRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setup_addon(self,api=asyncio):
        addon=actual_type(api)();addon.running=True;addon.ha_websocket=None;addon.ws_message_id=0
        addon.automation_refresh_wake=asyncio.Event();addon.scene_restore_requested=asyncio.Event()
        addon.log_activity=Mock();addon.fetch_energy_prefs=AsyncMock()
        return addon

    async def test_every_disconnect_is_paced_and_healthy_connection_resets_backoff(self):
        delays=[]
        async def sleep(seconds):delays.append(seconds)
        api=SimpleNamespace(sleep=sleep,CancelledError=asyncio.CancelledError)
        addon=self.setup_addon(api);attempts=0
        async def connect():
            nonlocal attempts
            attempts+=1
            if attempts==5:addon.running=False
            return attempts==3
        addon.connect_ha_websocket=AsyncMock(side_effect=connect)
        await addon.ha_websocket_loop()
        self.assertEqual(delays,[2,4,2,4]);self.assertEqual(attempts,5)

    async def test_503_and_failed_auth_return_unhealthy_without_recovery_or_preferences(self):
        addon=self.setup_addon();addon.session=SimpleNamespace(ws_connect=AsyncMock(side_effect=OSError('503')))
        self.assertFalse(await addon.connect_ha_websocket());self.assertFalse(addon.scene_restore_requested.is_set())
        socket=EmptySocket([{'type':'auth_required'},{'type':'auth_invalid'}]);addon.session.ws_connect=AsyncMock(return_value=socket)
        self.assertFalse(await addon.connect_ha_websocket());addon.fetch_energy_prefs.assert_not_awaited();socket.close.assert_awaited_once()

    async def test_authenticated_connection_wakes_existing_worker_and_only_long_healthy_session_resets_retry(self):
        clock=Mock(side_effect=[10,45]);api=SimpleNamespace(get_running_loop=lambda:SimpleNamespace(time=clock),wait_for=asyncio.wait_for,CancelledError=asyncio.CancelledError)
        addon=self.setup_addon(api);socket=EmptySocket([{'type':'auth_required'},{'type':'auth_ok'}]);addon.session=SimpleNamespace(ws_connect=AsyncMock(return_value=socket))
        self.assertTrue(await addon.connect_ha_websocket());self.assertTrue(addon.scene_restore_requested.is_set());self.assertTrue(addon.automation_refresh_wake.is_set())
        addon.fetch_energy_prefs.assert_awaited_once();self.assertIsNone(addon.ha_websocket)

    async def test_ha_started_coalesces_restore_wakes_without_tasks_or_activation(self):
        addon=self.setup_addon()
        for _ in range(100):await addon.handle_ws_message({'type':'event','event':{'event_type':'homeassistant_started'}})
        self.assertTrue(addon.scene_restore_requested.is_set());self.assertTrue(addon.automation_refresh_wake.is_set())

    async def test_delayed_core_read_keeps_restore_pending_until_bounded_worker_can_restore(self):
        addon=self.setup_addon();addon.scene_restore_requested.set();addon.get_ha_states=AsyncMock(side_effect=[[],[{'entity_id':'light.office'}]])
        addon.restore_managed_scenes=AsyncMock(return_value=True);addon.push_scenes_to_cloud=AsyncMock()
        await addon.refresh_automation_bindings();self.assertTrue(addon.scene_restore_requested.is_set())
        await addon.refresh_automation_bindings();self.assertFalse(addon.scene_restore_requested.is_set());addon.restore_managed_scenes.assert_awaited_once()
        addon.push_scenes_to_cloud.assert_not_awaited()

    async def test_cancellation_propagates_and_no_reconnect_is_started(self):
        addon=self.setup_addon();addon.connect_ha_websocket=AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):await addon.ha_websocket_loop()
        addon.connect_ha_websocket.assert_awaited_once()

    async def test_failed_restoration_is_limited_to_five_passes_without_self_waking(self):
        addon=self.setup_addon();addon.scene_restore_requested.set();addon.automation_refresh_wake.set()
        async def wait_for(awaitable,timeout):
            if getattr(getattr(awaitable,'cr_code',None),'co_name',None)=='wait' and not addon.automation_refresh_wake.is_set():
                awaitable.close();raise asyncio.TimeoutError
            return await awaitable
        addon.automation_refresh_loop.__func__.__globals__['asyncio']=SimpleNamespace(wait_for=wait_for,TimeoutError=asyncio.TimeoutError,CancelledError=asyncio.CancelledError)
        addon.get_ha_states=AsyncMock(return_value=[{'entity_id':'light.office'}]);count=0
        async def restore(states):
            nonlocal count
            count+=1
            if count==5:addon.running=False
            return False
        addon.restore_managed_scenes=AsyncMock(side_effect=restore)
        await addon.automation_refresh_loop()
        self.assertEqual(count,5);self.assertTrue(addon.scene_restore_requested.is_set());self.assertFalse(addon.automation_refresh_wake.is_set())
