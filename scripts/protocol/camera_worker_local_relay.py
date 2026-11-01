"""Offline only: actual worker + generated HA-shaped HTTP + demo relay handler.

Requires the separate demo-lapahub-camera-local bridge on localhost:8765.
All credentials below are deliberately synthetic and only work in that fixture.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lapahub/rootfs/app'))
from camera_capture import CameraCaptureWorker

RELAY = 'http://127.0.0.1:8765'
VIEWER = {'Authorization': 'Bearer synthetic-local-camera-viewer'}


async def main():
    calls = []
    source_status = 200
    images = [(ROOT / ('tests/fixtures/' + name)).read_bytes()
              for name in ('camera_generated.jpg', 'camera_generated_next.jpg')]
    known = {hashlib.sha256(value).hexdigest() for value in images}

    async def camera(request):
        assert request.headers['Authorization'] == 'Bearer synthetic-generated-ha'
        assert request.match_info['entity'] == 'camera.generated'
        assert dict(request.query) == {'width': '640', 'height': '360'}
        calls.append(source_status)
        return web.Response(body=images[(len(calls) - 1) % 2] if source_status == 200 else b'',
                            status=source_status, content_type='image/jpg')

    app = web.Application()
    app.router.add_get('/api/camera_proxy/{entity}', camera)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    ha = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    view_id = None
    trace = []
    tracing = aiohttp.TraceConfig()
    async def ended(session, context, params):
        trace.append({'method': params.method, 'path': params.url.path, 'status': params.response.status})
    tracing.on_request_end.append(ended)
    try:
        async with aiohttp.ClientSession(trust_env=False, trace_configs=[tracing]) as session:
            async with session.post(RELAY + '/view/open', headers=VIEWER, json={
                    'ownerId': 'generated-camera-owner', 'hubDocId': 'generated', 'deviceId': 'camera_generated'}) as response:
                assert response.status == 200, response.status
                view = await response.json()
                view_id = view['viewId']
            worker = CameraCaptureWorker(session, ha, 'synthetic-generated-ha', RELAY,
                'generated-camera-hub', lambda: 'synthetic-local-camera-publisher')
            frames = []
            for _ in range(2):
                await worker.step()
                async with session.get(RELAY + '/view/frame', headers=VIEWER,
                        params={'viewId': view_id, 'after': frames[-1]['sequence'] if frames else 0}) as response:
                    assert response.status == 200, response.status
                    data = await response.read()
                    digest = hashlib.sha256(data).hexdigest()
                    assert digest in known
                    assert response.headers['Content-Type'].startswith('image/jpeg')
                    assert response.headers['X-Lapa-Frame-State'] == 'fresh'
                    frames.append({'sequence': int(response.headers['X-Lapa-Frame-Sequence']),
                        'hash': digest, 'bytes': len(data), 'observedAt': response.headers['X-Lapa-Observed-At']})
                await asyncio.sleep(1.05)
            assert frames[1]['sequence'] > frames[0]['sequence'] and frames[1]['hash'] != frames[0]['hash']
            source_status = 503
            await worker.step()
            async with session.get(RELAY + '/view/frame', headers=VIEWER, params={'viewId': view_id}) as response:
                off_status = response.status
                assert off_status == 503, off_status
            count = len(calls)
            await asyncio.sleep(1.05)
            await worker.step()
            assert len(calls) == count, 'Off generation captured again'
            async with session.post(RELAY + '/view/close', headers=VIEWER, json={'viewId': view_id}) as response:
                assert response.status == 200
            await worker.step()
            assert len(calls) == count and not worker.bindings
            print(json.dumps({'result': 'pass', 'fixture': 'demo-lapahub-camera-local',
                'ha': 'generated HTTP fixture, not official HA', 'relay': 'actual production handler and Firestore emulator store; fake fixture identity',
                'frames': frames, 'off_status': off_status, 'off_generation_stopped': True,
                'close_removed_demand': True, 'scope': 'offline worker/relay integration only'}))
    except Exception:
        print(json.dumps({'trace': trace, 'ha_calls': calls, 'blocked': list(worker.blocked) if 'worker' in locals() else []}), file=sys.stderr)
        raise
    finally:
        if view_id:
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.post(RELAY + '/view/close', headers=VIEWER, json={'viewId': view_id}):
                    pass
        await runner.cleanup()


async def publisher_only():
    # Bounded companion for the Chromium fixture; never creates a viewer lease.
    calls, hashes = 0, set()
    enabled = True
    images = [(ROOT / ('tests/fixtures/' + name)).read_bytes()
              for name in ('camera_generated.jpg', 'camera_generated_next.jpg')]
    async def camera(request):
        nonlocal calls
        assert request.headers['Authorization'] == 'Bearer synthetic-generated-ha'
        assert request.match_info['entity'] == 'camera.generated'
        calls += 1
        data = images[calls % 2] if enabled else b''
        if data:
            hashes.add(hashlib.sha256(data).hexdigest())
        return web.Response(body=data, status=200 if enabled else 503, content_type='image/jpg')
    async def stats(request):
        return web.json_response({'captures': calls, 'hashes': sorted(hashes), 'enabled': enabled})
    async def off(request):
        nonlocal enabled
        enabled = False
        return web.json_response({'enabled': False})
    app = web.Application()
    app.router.add_get('/api/camera_proxy/{entity}', camera)
    app.router.add_get('/fixture/stats', stats)
    app.router.add_post('/fixture/off', off)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    ha = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    deadline = time.monotonic() + 240
    print(json.dumps({'publisher_ready': True, 'generated_source': ha, 'max_seconds': 240}), flush=True)
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            worker = CameraCaptureWorker(session, ha, 'synthetic-generated-ha', RELAY,
                'generated-camera-hub', lambda: 'synthetic-local-camera-publisher',
                running=lambda: time.monotonic() < deadline)
            await worker.run()
    finally:
        await runner.cleanup()
        print(json.dumps({'publisher_stopped': True, 'captures': calls, 'hashes': sorted(hashes)}), flush=True)


asyncio.run(publisher_only() if '--publisher-only' in sys.argv else main())
