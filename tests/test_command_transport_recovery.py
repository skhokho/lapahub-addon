"""Offline transport faults using actual addon methods and generated HTTP peers."""
import ast
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import unittest
import aiohttp
from aiohttp import web
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app'))
from reliability import CommandJournal, CommandExpired, check_command_deadline


def actual_type(api=asyncio):
    source=Path(__file__).resolve().parents[1]/'lapahub/rootfs/app/run.py'
    cls=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='LapaHubAddon')
    names={'command_listener_loop','_open_command_stream','_stream_commands_sse','_poll_commands_classic',
           '_handle_sse_event','execute_command','_execute_command_once','command_transport_status'}
    ns=dict(asyncio=api,datetime=datetime,timezone=timezone,json=json,aiohttp=aiohttp,
            logger=logging.getLogger('transport-test'),CommandJournal=CommandJournal,CommandExpired=CommandExpired,
            check_command_deadline=check_command_deadline)
    methods=[m for m in cls.body if isinstance(m,(ast.AsyncFunctionDef,ast.FunctionDef)) and m.name in names]
    for method in methods:exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),str(source),'exec'),ns)
    return type('ActualTransportMethods',(),{m.name:ns[m.name] for m in methods})


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setup_clock(self):
        self.clock=0.;self.sleeps=[]
        async def sleep(seconds):self.clock+=seconds;self.sleeps.append(seconds)
        return SimpleNamespace(CancelledError=asyncio.CancelledError,sleep=sleep,
            get_running_loop=lambda:SimpleNamespace(time=lambda:self.clock))

    def addon(self,api=asyncio):
        addon=actual_type(api)();addon.running=True;addon.firebase_credentials={'token':'synthetic-one'}
        addon.firebase_project='synthetic';addon.hub_id='synthetic';addon.log_activity=Mock()
        addon.authenticate_with_retry=AsyncMock();return addon

    async def test_successful_polling_eventually_retries_sse_without_poll_failure(self):
        addon=self.addon(self.setup_clock());attempts=[];polls=[]
        async def stream():
            attempts.append(self.clock)
            if len(attempts)<=3:raise RuntimeError('Synthetic stream failure')
            addon.running=False
        async def poll():
            polls.append(self.clock)
            if len(polls)>100:addon.running=False
        addon._stream_commands_sse=stream;addon._poll_commands_classic=poll
        await addon.command_listener_loop()
        self.assertEqual(len(attempts),4)
        self.assertGreaterEqual(attempts[-1]-attempts[2],30)
        self.assertGreater(len(polls),10)
        addon.authenticate_with_retry.assert_not_awaited()

    async def test_failed_probes_backoff_to_300_seconds_while_polling_continues(self):
        addon=self.addon(self.setup_clock());attempts=[];polls=0
        async def stream():
            attempts.append(self.clock)
            if len(attempts)>=9:addon.running=False
            raise RuntimeError('Synthetic stream failure')
        async def poll():
            nonlocal polls
            polls+=1
            if polls>2000:addon.running=False
        addon._stream_commands_sse=stream;addon._poll_commands_classic=poll
        await addon.command_listener_loop()
        gaps=[attempts[i]-attempts[i-1] for i in range(3,len(attempts))]
        self.assertEqual(gaps,[30,60,120,240,300,300])
        self.assertGreater(polls,900)

    async def test_new_token_generation_retries_after_completed_poll_without_waiting_30s(self):
        addon=self.addon(self.setup_clock());attempts=[];poll_finished=[]
        async def stream():
            attempts.append(self.clock)
            if len(attempts)<=3:raise RuntimeError('Synthetic stream failure')
            self.assertTrue(poll_finished[-1]);addon.running=False
        async def poll():
            addon.firebase_credentials={'token':'synthetic-two'}
            poll_finished.append(True)
            if len(poll_finished)>100:addon.running=False
        addon._stream_commands_sse=stream;addon._poll_commands_classic=poll
        await addon.command_listener_loop()
        self.assertEqual(len(attempts),4)
        self.assertLess(attempts[-1]-attempts[2],30)

    async def test_immediate_clean_eof_is_paced_and_enters_fallback(self):
        addon=self.addon(self.setup_clock());calls=0
        async def stream():
            nonlocal calls
            calls+=1
            if calls>5:addon.running=False
        async def poll():addon.running=False
        addon._stream_commands_sse=stream;addon._poll_commands_classic=poll
        await addon.command_listener_loop()
        self.assertEqual(calls,3)
        self.assertGreaterEqual(sum(self.sleeps),2)
        status=addon.command_transport_status()
        self.assertEqual(status['mode'],'stopped')
        self.assertNotIn('synthetic-one',json.dumps(status))
        self.assertNotIn('Authorization',json.dumps(status))

    async def test_token_change_never_cancels_or_overlaps_inflight_poll(self):
        addon=self.addon(self.setup_clock());attempts=[];entered=asyncio.Event();release=asyncio.Event()
        async def stream():
            self.assertTrue(not entered.is_set() or release.is_set())
            attempts.append(self.clock)
            if len(attempts)<=3:raise RuntimeError('Synthetic stream failure')
            addon.running=False
        async def poll():
            entered.set();await release.wait()
            if self.clock>100:addon.running=False
        addon._stream_commands_sse=stream;addon._poll_commands_classic=poll
        task=asyncio.create_task(addon.command_listener_loop());await entered.wait()
        addon.firebase_credentials={'token':'synthetic-two'}
        await asyncio.sleep(0)
        self.assertEqual(len(attempts),3)
        self.assertFalse(task.done())
        release.set();await task
        self.assertEqual(len(attempts),4)

    async def test_missing_credentials_are_not_counted_as_successful_polls(self):
        api=self.setup_clock();addon=self.addon(api);addon.firebase_credentials=None
        async def sleep(seconds):addon.running=False
        api.sleep=sleep
        addon._stream_commands_sse=AsyncMock();addon._poll_commands_classic=AsyncMock()
        await addon.command_listener_loop()
        addon._stream_commands_sse.assert_not_awaited();addon._poll_commands_classic.assert_not_awaited()
        status=addon.command_transport_status()
        self.assertEqual(status['poll_successes'],0)
        self.assertIsNone(status['last_contact_at'])


class HttpFaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gate=asyncio.Event();self.entered=asyncio.Event();self.claimed=False;self.polls=0;self.immediate_headers=False
        self.command={'id':'expired-transport-intent','entity_id':'light.fixture','action':'turn_on','params':{},'expires_at':time.time()+.05}
        async def stream(request):
            self.claimed=True;self.entered.set()
            if self.immediate_headers:
                response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
                await response.prepare(request);await self.gate.wait()
                try:await response.write(b'event: heartbeat\ndata: {}\n\n')
                except ConnectionResetError:pass
                return response
            await self.gate.wait()
            return web.Response(text='event: commands\ndata: '+json.dumps({'commands':[self.command]})+'\n\n',content_type='text/event-stream')
        async def poll(request):self.polls+=1;return web.json_response({'commands':[self.command]})
        app=web.Application();app.router.add_get('/waitForCommands',stream);app.router.add_get('/getPendingCommands',poll)
        self.runner=web.AppRunner(app);await self.runner.setup();site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.base='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1]);self.session=aiohttp.ClientSession(trust_env=False)
        class Gateway:
            def get(inner,url,**kwargs):return self.session.get(self.base+'/'+url.rsplit('/',1)[-1],**kwargs)
        self.addon=actual_type()();self.addon.running=True;self.addon.firebase_credentials={'token':'synthetic'}
        self.addon.firebase_project='synthetic';self.addon.hub_id='synthetic';self.addon.session=Gateway();self.addon.log_activity=Mock()
        self.addon.command_transport={};self.temp=tempfile.TemporaryDirectory();self.addon.command_journal=CommandJournal(Path(self.temp.name)/'journal.sqlite3')
        self.addon.call_ha_service=AsyncMock(side_effect=AssertionError('No stale device service'))

    async def asyncTearDown(self):
        self.gate.set();await self.session.close();await self.runner.cleanup();self.addon.command_journal.close();self.temp.cleanup()

    async def test_hung_headers_are_bounded_and_late_claim_never_executes(self):
        started=time.monotonic()
        with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(self.addon._stream_commands_sse(),1.5)
        self.assertLess(time.monotonic()-started,1.4)
        self.assertTrue(self.claimed)
        self.gate.set()
        await self.addon._poll_commands_classic()
        self.assertEqual(self.polls,1)
        self.addon.call_ha_service.assert_not_awaited()
        self.assertEqual(self.addon.command_journal.pending(),[('expired-transport-intent','expired')])

    async def test_shutdown_during_header_wait_cancels_and_closes_request(self):
        task=asyncio.create_task(self.addon._stream_commands_sse());await self.entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(self.session.closed)
        self.command["expires_at"]=time.time()-1
        self.gate.set();await self.addon._poll_commands_classic()
        self.addon.call_ha_service.assert_not_awaited()

    async def test_open_header_budget_does_not_shorten_established_stream_read(self):
        self.immediate_headers=True
        task=asyncio.create_task(self.addon._stream_commands_sse())
        await self.entered.wait()
        await asyncio.sleep(1.05)
        self.assertFalse(task.done())
        self.assertTrue(self.addon.command_transport['http_connected'])
        self.assertEqual(self.addon.command_transport['mode'],'sse')
        self.addon.running=False;self.gate.set();await task
        self.assertFalse(self.addon.command_transport['http_connected'])
        self.assertIsNotNone(self.addon.command_transport['last_contact_at'])

    async def test_cancel_established_stream_releases_socket_without_closing_shared_session(self):
        self.immediate_headers=True
        task=asyncio.create_task(self.addon._stream_commands_sse());await self.entered.wait()
        for _ in range(20):
            if self.addon.command_transport.get('http_connected'):break
            await asyncio.sleep(.005)
        self.assertTrue(self.addon.command_transport.get('http_connected'))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(self.addon.command_transport['http_connected'])
        self.assertFalse(self.session.closed)
        self.assertEqual(len(self.session.connector._acquired),0)
