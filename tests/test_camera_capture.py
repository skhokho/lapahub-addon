"""Bounded JPEG decoding and lease-driven capture; never uses a home camera."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lapahub/rootfs/app'))
from camera_capture import CameraFrameError, MAX_FRAME_BYTES, validate_jpeg


def jpeg(size=(160, 96)):
    # Synthetic fixture encoded once; runtime/tests need no image library.
    data = bytearray((Path(__file__).parent / 'fixtures/camera_generated.jpg').read_bytes())
    start = data.index(b'\xff\xc0') + 2
    data[start + 3:start + 5] = size[1].to_bytes(2, 'big')
    data[start + 5:start + 7] = size[0].to_bytes(2, 'big')
    return bytes(data)


class JpegTests(unittest.TestCase):
    def test_decode_real_generated_frame_and_jpg_alias(self):
        for mime in ('image/jpeg', 'image/jpg', 'image/jpeg; charset=binary'):
            self.assertEqual(validate_jpeg(jpeg(), mime), (160, 96))

    def test_progressive_jpeg_and_malformed_segments(self):
        data = (Path(__file__).parent / 'fixtures/camera_generated_progressive.jpg').read_bytes()
        self.assertEqual(validate_jpeg(data, 'image/jpeg'), (160, 96))
        broken = bytearray(jpeg())
        broken[4:6] = b'\x00\x01'
        for value in (bytes(broken), jpeg() + b'trailing', b'\xff\xd8\xff\xd9'):
            with self.assertRaises(CameraFrameError):
                validate_jpeg(value, 'image/jpeg')

    def test_non_image_and_oversize_dimensions_or_bytes_are_rejected(self):
        for data, mime in ((b'html', 'image/jpeg'), (jpeg(), 'text/html'), (jpeg(), None),
                           (jpeg()[:-2], 'image/jpeg'), (b'\xff\xd8' + b'x' * MAX_FRAME_BYTES, 'image/jpeg'),
                           (jpeg((1921, 1)), 'image/jpeg'), (jpeg((1, 1081)), 'image/jpeg')):
            with self.subTest(mime=mime, bytes=len(data)):
                with self.assertRaises(CameraFrameError):
                    validate_jpeg(data, mime)

import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import tempfile
import aiohttp
from aiohttp import web
from camera_capture import CameraCaptureWorker


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = 0.0
        self.base_time = datetime.now(timezone.utc)
        self.camera_calls, self.uploads, self.statuses = [], [], []
        self.demand_status = self.frame_status = self.ha_status = 200
        self.gate = self.entered = None
        self.chunked = False
        self.active = self.max_active = 0
        self.image = jpeg()
        self.content_type = 'image/jpg'
        self.views = [self.view('view-one')]
        app = web.Application()
        app.router.add_post('/hub/demand', self.demand)
        app.router.add_post('/hub/frame', self.frame)
        app.router.add_post('/hub/status', self.status)
        app.router.add_get('/api/camera_proxy/{entity}', self.camera)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        self.session = aiohttp.ClientSession(trust_env=False)
        self.worker = CameraCaptureWorker(self.session, self.url, 'fixture-ha-secret', self.url,
            'fixture-hub', lambda: 'fixture-cloud-secret', now=lambda: self.base_time + timedelta(seconds=self.clock),
            monotonic=lambda: self.clock)

    def view(self, view_id, sequence=7):
        return {'viewId': view_id, 'entityId': 'camera.fixture',
            'expiresAt': (self.base_time + timedelta(seconds=60)).isoformat(), 'lastSequence': sequence,
            'requestedWidth': 640, 'requestedHeight': 360, 'minCaptureIntervalMs': 1000, 'maxFrameBytes': 524288}

    async def asyncTearDown(self):
        if self.gate:
            self.gate.set()
        await self.session.close()
        await self.runner.cleanup()

    async def demand(self, request):
        self.assertEqual(request.headers['Authorization'], 'Bearer fixture-cloud-secret')
        self.assertEqual(await request.json(), {'hubId': 'fixture-hub'})
        return web.json_response({'views': self.views, 'pollIntervalMs': 1000}, status=self.demand_status)

    async def camera(self, request):
        self.assertEqual(request.headers['Authorization'], 'Bearer fixture-ha-secret')
        self.assertEqual(dict(request.query), {'width': '640', 'height': '360'})
        self.camera_calls.append(request.match_info['entity'])
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.entered:
                self.entered.set()
            if self.gate:
                await self.gate.wait()
            if self.chunked:
                response = web.StreamResponse(headers={'Content-Type': self.content_type})
                await response.prepare(request)
                try:
                    for offset in range(0, len(self.image), 16384):
                        await response.write(self.image[offset:offset + 16384])
                except ConnectionResetError:
                    pass
                return response
            return web.Response(body=self.image, content_type=self.content_type, status=self.ha_status)
        finally:
            self.active -= 1

    async def frame(self, request):
        self.assertEqual(request.headers['Authorization'], 'Bearer fixture-cloud-secret')
        self.assertNotIn('fixture-ha-secret', str(request.headers))
        self.assertEqual(request.content_type, 'image/jpeg')
        self.assertRegex(request.headers['X-Lapa-Observed-At'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
        data = await request.read()
        self.uploads.append({'view': request.query['viewId'], 'sequence': int(request.headers['X-Lapa-Frame-Sequence']),
            'observed': request.headers['X-Lapa-Observed-At'], 'hash': hashlib.sha256(data).hexdigest()})
        return web.json_response({'accepted': self.frame_status == 200}, status=self.frame_status)

    async def status(self, request):
        self.assertEqual(request.headers['Authorization'], 'Bearer fixture-cloud-secret')
        self.statuses.append(await request.json())
        return web.json_response({})

    async def test_real_http_generated_jpeg_sequence_rate_and_empty_demand(self):
        await self.worker.step()
        self.assertEqual(self.camera_calls, ['camera.fixture'])
        self.assertEqual(self.uploads[0]['sequence'], 8)
        self.assertEqual(self.uploads[0]['hash'], hashlib.sha256(self.image).hexdigest())
        await self.worker.step()
        self.assertEqual(len(self.camera_calls), 1)
        self.clock += 1.1
        self.views[0]['lastSequence'] = 20
        await self.worker.step()
        self.assertEqual(self.uploads[-1]['sequence'], 21)
        self.views = []
        await self.worker.step()
        self.assertEqual(len(self.camera_calls), 2)
        self.assertEqual(self.worker.sequences, {})

    async def test_revocation_during_capture_stops_same_generation(self):
        self.gate, self.entered = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.worker.step())
        await self.entered.wait()
        self.frame_status = 403
        self.gate.set()
        await task
        self.clock += 2
        await self.worker.step()
        self.assertEqual(len(self.camera_calls), 1)
        self.assertIn('view-one', self.worker.blocked)
        self.frame_status = 200
        self.views = [self.view('new-view')]
        await self.worker.step()
        self.assertEqual(len(self.camera_calls), 2)

    async def test_expiry_during_hung_capture_never_uploads(self):
        self.gate, self.entered = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.worker.step())
        await self.entered.wait()
        self.clock += 61
        self.gate.set()
        await task
        self.assertEqual(self.uploads, [])

    async def test_camera_off_and_invalid_image_require_new_intent(self):
        self.ha_status = 503
        await self.worker.step()
        self.assertEqual(self.statuses[-1]['state'], 'off')
        self.ha_status = 200
        self.clock += 2
        await self.worker.step()
        self.assertEqual(len(self.camera_calls), 1)
        self.views = [self.view('bad-image')]
        self.image = b'<html>not a camera frame</html>'
        await self.worker.step()
        self.assertEqual(self.statuses[-1]['state'], 'error')
        self.assertEqual(self.uploads, [])

    async def test_transient_demand_failure_reauthorizes_before_new_capture(self):
        await self.worker.step()
        self.clock += 2
        self.demand_status = 503
        await self.worker.step()
        self.assertEqual(len(self.camera_calls), 1)
        self.demand_status = 200
        await self.worker.step()
        self.assertEqual(len(self.camera_calls), 2)

    async def test_untrusted_entity_url_and_oversized_metadata_fail_closed(self):
        self.views[0]['entityId'] = 'camera.fixture/../../secret?token=bad'
        await self.worker.step()
        self.assertEqual(self.camera_calls, [])
        self.views = [self.view('v' + str(i)) for i in range(5)]
        await self.worker.step()
        self.assertEqual(self.camera_calls, [])

    async def test_chunked_oversized_body_never_uploads(self):
        self.chunked = True
        self.image = b'\xff\xd8' + b'x' * (MAX_FRAME_BYTES + 1)
        await self.worker.step()
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.statuses[-1]['state'], 'error')

    async def test_four_views_capture_at_most_two_at_once(self):
        self.views = [self.view('view-' + str(i)) for i in range(4)]
        self.gate, self.entered = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.worker.step())
        await self.entered.wait()
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(len(self.camera_calls), 2)
        self.gate.set()
        await task
        self.assertEqual(len(self.uploads), 4)
        self.assertEqual(self.max_active, 2)

    async def test_cancelled_capture_leaves_both_databases_identical(self):
        import sqlite3
        from reliability import CommandJournal
        from managed_scenes import ManagedScenes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = CommandJournal(root / 'command_journal.sqlite3')
            journal.claim({'id': 'preserved', 'entity_id': 'light.fixture', 'action': 'turn_on', 'params': {}})
            journal.complete('preserved', 'service_accepted')
            scenes = ManagedScenes(root / 'managed_scenes.sqlite3')
            scenes.save('scene.lapahub_fixture', {'scene_id': 'lapahub_fixture', 'entities': {'light.fixture': {'state': 'off'}}})
            scenes.suppress('scene.lapahub_fixture')
            journal.close()
            scenes.close()
            paths = list(root.glob('*.sqlite3'))
            before = {path.name: (path.stat().st_ino, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths}
            self.gate, self.entered = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(self.worker.step())
            await self.entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.gate.set()
            self.assertEqual(self.uploads, [])
            for path in paths:
                self.assertEqual((path.stat().st_ino, hashlib.sha256(path.read_bytes()).hexdigest()), before[path.name])
                with sqlite3.connect(path) as db:
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            journal = CommandJournal(root / 'command_journal.sqlite3')
            self.assertIn(('preserved', 'service_accepted'), journal.pending())
            self.assertFalse(journal.claim({'id': 'preserved', 'entity_id': 'light.fixture', 'action': 'turn_on', 'params': {}}))
            scenes = ManagedScenes(root / 'managed_scenes.sqlite3')
            self.assertEqual(scenes.active(), {})
            journal.close()
            scenes.close()

    async def test_existing_view_cannot_rebind_to_another_camera(self):
        await self.worker.step()
        self.clock += 2
        self.views[0]['entityId'] = 'camera.another'
        await self.worker.step()
        self.assertEqual(self.camera_calls, ['camera.fixture'])

    async def test_source_authentication_failure_stops_without_forwarding_body(self):
        self.ha_status = 401
        self.image = b'private diagnostic body'
        await self.worker.step()
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.statuses, [{'hubId': 'fixture-hub', 'viewId': 'view-one', 'state': 'unavailable'}])
        self.clock += 2
        self.ha_status = 200
        await self.worker.step()
        self.assertEqual(len(self.camera_calls), 1)
