"""A rejected classic poll must reach the actual listener's recovery policy."""
import ast
import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import unittest


def actual_type(asyncio_api=asyncio):
    source = Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app/run.py'
    cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'LapaHubAddon')
    names = {'_poll_commands_classic', 'command_listener_loop'}
    namespace = {'asyncio': asyncio_api, 'logger': logging.getLogger('poll-test'), 'datetime': datetime,
                 'timezone': timezone, 'aiohttp': SimpleNamespace(ClientTimeout=lambda **kw: kw)}
    for method in cls.body:
        if isinstance(method, ast.AsyncFunctionDef) and method.name in names:
            exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), namespace)
    return type('ActualPollMethods', (), {name: namespace[name] for name in names})


class Response:
    def __init__(self, status):
        self.status = status
        self.json = AsyncMock(return_value={'commands': []})
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None


class PollErrorTests(unittest.IsolatedAsyncioTestCase):
    def addon(self):
        addon = actual_type()()
        addon.firebase_credentials = {'token': 'synthetic-session'}
        addon.firebase_project = 'synthetic-fixture'
        addon.hub_id = 'synthetic-hub'
        addon.execute_command = AsyncMock()
        return addon

    async def test_non_success_status_is_not_a_successful_empty_poll(self):
        addon = self.addon()
        for status in (401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                response = Response(status)
                addon.session = SimpleNamespace(get=Mock(return_value=response))
                with self.assertRaisesRegex(RuntimeError, str(status)):
                    await addon._poll_commands_classic()
                response.json.assert_not_awaited()
                addon.execute_command.assert_not_awaited()

    async def test_successful_empty_poll_remains_successful(self):
        addon = self.addon()
        addon.session = SimpleNamespace(get=Mock(return_value=Response(200)))
        await addon._poll_commands_classic()
        addon.execute_command.assert_not_awaited()

    async def test_real_listener_reauthenticates_after_sse_then_rejected_poll(self):
        api = SimpleNamespace(CancelledError=asyncio.CancelledError, sleep=AsyncMock(), get_running_loop=asyncio.get_running_loop)
        addon = actual_type(api)()
        addon.firebase_credentials = {'token': 'synthetic-invalid-session'}
        addon.firebase_project = 'synthetic-fixture'
        addon.hub_id = 'synthetic-hub'
        addon.running = True
        addon.log_activity = Mock()
        addon._stream_commands_sse = AsyncMock(side_effect=RuntimeError('Synthetic SSE rejection'))
        polls = 0
        def get(*args, **kwargs):
            nonlocal polls
            polls += 1
            if polls >= 6: addon.running = False  # Bound the pre-fix regression.
            return Response(401)
        addon.session = SimpleNamespace(get=get)
        async def authenticated(): addon.running = False
        addon.authenticate_with_retry = AsyncMock(side_effect=authenticated)
        await addon.command_listener_loop()
        addon.authenticate_with_retry.assert_awaited_once()
        self.assertEqual(polls, 5)
