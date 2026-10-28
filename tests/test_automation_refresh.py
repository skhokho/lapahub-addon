"""Exercise the actual shared worker without HA, sleeps, or physical services."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from test_automation_config import actual_type


class AutomationRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.addon = actual_type()()
        self.addon.running = True
        self.addon.automation_refresh_wake = asyncio.Event()
        self.addon.scene_restore_requested = asyncio.Event()
        self.addon.call_ha_service = AsyncMock(side_effect=AssertionError('physical service forbidden'))
        self.addon.restore_managed_scenes = AsyncMock(side_effect=AssertionError('scene restoration forbidden'))
        self.addon.push_scenes_to_cloud = AsyncMock()
        self.addon.enrich_automation_configs = AsyncMock(side_effect=lambda items: [dict(item, config_id='lapahub_test', observed_config={'id': 'lapahub_test'}) for item in items])
        self.timeouts = []

    def fast_bounded_clock(self):
        async def wait_for(awaitable, timeout):
            self.timeouts.append(timeout)
            if getattr(getattr(awaitable, 'cr_code', None), 'co_name', None) == 'wait' and not self.addon.automation_refresh_wake.is_set():
                awaitable.close()
                raise asyncio.TimeoutError
            return await awaitable
        self.addon.automation_refresh_loop.__func__.__globals__['asyncio'] = SimpleNamespace(
            wait_for=wait_for, TimeoutError=asyncio.TimeoutError, CancelledError=asyncio.CancelledError)

    async def test_delayed_reload_is_observed_by_bounded_read_only_retries(self):
        self.fast_bounded_clock()
        reads = 0
        async def states():
            nonlocal reads
            reads += 1
            if reads == 5:
                self.addon.running = False
            # The async HA reload has not yet created its entity on first reads.
            if reads < 3:
                return [{'entity_id': 'light.office', 'state': 'off'}]
            return [{'entity_id': 'scene.evening'}, {'entity_id': 'automation.office_alias', 'state': 'off', 'attributes': {'friendly_name': 'Office'}}]
        self.addon.get_ha_states = AsyncMock(side_effect=states)
        self.addon.automation_refresh_wake.set()
        await self.addon.automation_refresh_loop()
        self.assertEqual(reads, 5)
        self.assertEqual(self.timeouts, [8, 1, 8, 2, 8, 4, 8, 8, 8])
        self.assertEqual(self.addon.push_scenes_to_cloud.await_count, 3)
        for call in self.addon.push_scenes_to_cloud.await_args_list:
            self.assertEqual(call.args[0], [])
            self.assertEqual(call.args[1][0]['entity_id'], 'automation.office_alias')
            self.assertEqual(call.args[1][0]['config_id'], 'lapahub_test')
        self.addon.call_ha_service.assert_not_awaited()
        self.addon.restore_managed_scenes.assert_not_awaited()

    async def test_rapid_edits_coalesce_without_task_fanout_or_retry_reset(self):
        self.fast_bounded_clock()
        passes = 0
        async def refresh():
            nonlocal passes
            passes += 1
            if passes < 5:
                for _ in range(100):
                    self.addon.automation_refresh_wake.set()
            else:
                self.addon.running = False
        self.addon.refresh_automation_bindings = AsyncMock(side_effect=refresh)
        self.addon.automation_refresh_wake.set()
        await self.addon.automation_refresh_loop()
        self.assertEqual(passes, 5)
        self.assertFalse(self.addon.automation_refresh_wake.is_set())

    async def test_idle_worker_does_not_poll_and_shutdown_wake_exits(self):
        self.addon.refresh_automation_bindings = AsyncMock()
        task = asyncio.create_task(self.addon.automation_refresh_loop())
        await asyncio.sleep(0)
        self.addon.refresh_automation_bindings.assert_not_awaited()
        self.addon.running = False
        self.addon.automation_refresh_wake.set()
        await task
        self.addon.refresh_automation_bindings.assert_not_awaited()

    async def test_shutdown_cancels_active_read_and_does_not_restart_worker(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def refresh():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.addon.refresh_automation_bindings = AsyncMock(side_effect=refresh)
        self.addon.automation_refresh_wake.set()
        task = asyncio.create_task(self.addon.automation_refresh_loop())
        await entered.wait()
        self.addon.running = False
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.addon.refresh_automation_bindings.assert_awaited_once()

    async def test_transient_refresh_errors_do_not_escape_or_make_unbounded_attempts(self):
        self.fast_bounded_clock()
        attempts = 0
        async def refresh():
            nonlocal attempts
            attempts += 1
            if attempts == 5:
                self.addon.running = False
            raise OSError('read-only HA offline')
        self.addon.refresh_automation_bindings = AsyncMock(side_effect=refresh)
        self.addon.automation_refresh_wake.set()
        await self.addon.automation_refresh_loop()
        self.assertEqual(attempts, 5)
