"""HA 2026.10.4 generated-pattern camera API probe; ephemeral container only.

No cloud client or home credentials are used. /fixture contains hashes of generated local patterns. No home images are read.
This checks HA image/MJPEG APIs, never app playback or physical camera behavior.
"""
import asyncio
import json
import hashlib
import io
from PIL import Image
from pathlib import Path
import secrets

import aiohttp


BASE = 'http://127.0.0.1:8123'
CLIENT = BASE + '/'


async def main():
    assert Path('/.dockerenv').exists(), 'Ephemeral Docker fixture required'
    assert Path('/config/configuration.yaml').read_text().startswith('homeassistant:\n  name: Isolated protocol fixture')
    async with aiohttp.ClientSession(trust_env=False) as session:
        async def request(method, path, **kwargs):
            async with session.request(method, BASE + path, **kwargs) as response:
                body = await response.json()
                assert response.status < 300, (path, response.status, list(body) if isinstance(body, dict) else type(body).__name__)
                return body
        token_path = Path('/config/.fixture-token')
        if token_path.exists():
            token = {'access_token': token_path.read_text()}
        else:
            user = await request('POST', '/api/onboarding/users', json={
                'client_id': CLIENT, 'name': 'Isolated fixture', 'username': 'isolated-fixture',
                'password': secrets.token_urlsafe(32), 'language': 'en'})
            token = await request('POST', '/auth/token', data={
                'grant_type': 'authorization_code', 'code': user['auth_code'], 'client_id': CLIENT})
            token_path.write_text(token['access_token'])
            token_path.chmod(0o600)
        headers = {'Authorization': 'Bearer ' + token['access_token']}
        config = await request('GET', '/api/config', headers=headers)
        assert config['version'] == '2026.10.4', config['version']
        for _ in range(60):
            states = await request('GET', '/api/states', headers=headers)
            cameras = [s for s in states if s['entity_id'].startswith('camera.') and 'png' not in s['entity_id'] and 'without' not in s['entity_id']]
            if cameras:
                break
            await asyncio.sleep(1)
        assert cameras, 'Official demo camera not loaded'
        entity = cameras[0]['entity_id']
        expected = json.loads(Path('/fixture/patterns.json').read_text())
        known = {item['sha256'] for item in expected}
        limit = 512 * 1024

        async def bounded(reader):
            data = bytearray()
            async for chunk in reader.iter_chunked(8192):
                assert len(data) + len(chunk) <= limit, 'Frame size limit exceeded'
                data.extend(chunk)
            return bytes(data)

        async def frame_metadata(data, content_type):
            digest = hashlib.sha256(data).hexdigest()
            assert digest in known, 'Received bytes are not a generated local fixture frame'
            assert data.startswith(b'\xff\xd8') and content_type.split(';')[0] in ('image/jpeg', 'image/jpg')
            image = Image.open(io.BytesIO(data))
            assert image.size == (160, 96)
            return {'content_type': content_type, 'bytes': len(data), 'sha256': digest, 'width': 160, 'height': 96}

        denied = {}
        for path in ('/api/camera_proxy/', '/api/camera_proxy_stream/'):
            async with session.get(BASE + path + entity) as response:
                denied[path] = response.status
                assert response.status == 403
        async with session.get(BASE + '/api/camera_proxy/' + entity, headers={'Authorization': 'Bearer invalid-fixture-token'}) as response:
            denied['invalid_bearer'] = response.status
            assert response.status == 401
        async with session.get(BASE + '/api/camera_proxy/' + entity, headers=headers) as response:
            assert response.status == 200
            snapshot = await frame_metadata(await bounded(response.content), response.headers['Content-Type'])
        frames = []
        async with session.get(BASE + '/api/camera_proxy_stream/' + entity, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15, sock_read=5)) as response:
            assert response.status == 200
            stream_type = response.headers['Content-Type']
            assert stream_type.startswith('multipart/x-mixed-replace')
            # HA2026.10.4 declares --frameboundary but sends that exact token
            # as the delimiter, rather than adding the RFC two-dash prefix.
            boundary = stream_type.split('boundary=', 1)[1].strip().strip('"').encode()
            first_delimiter = None
            for _ in range(3):
                line = await response.content.readline()
                while line in (b'\r\n', b'\n'):
                    line = await response.content.readline()
                assert len(line) <= 1024
                delimiter = line.rstrip(b'\r\n')
                assert delimiter in (boundary, b'--' + boundary)
                first_delimiter = first_delimiter or delimiter.decode()
                part_headers = {}
                header_bytes = 0
                while True:
                    line = await response.content.readline()
                    header_bytes += len(line)
                    assert header_bytes <= 4096
                    if line in (b'\r\n', b'\n'):
                        break
                    assert line and b':' in line
                    key, value = line.decode('ascii').split(':', 1)
                    part_headers[key.lower()] = value.strip()
                length = int(part_headers['content-length'])
                assert 0 < length <= limit
                data = await response.content.readexactly(length)
                assert await response.content.readexactly(2) == b'\r\n'
                frames.append(await frame_metadata(data, part_headers['content-type']))
        assert len({f['sha256'] for f in frames}) >= 2, 'Generated stream did not advance'
        async with session.ws_connect(BASE + '/api/websocket') as socket:
            assert (await socket.receive_json())['type'] == 'auth_required'
            await socket.send_json({'type': 'auth', 'access_token': token['access_token']})
            assert (await socket.receive_json())['type'] == 'auth_ok'
            await socket.send_json({'id': 1, 'type': 'camera/capabilities', 'entity_id': entity})
            capabilities = await socket.receive_json()
            assert capabilities['success']
            await socket.send_json({'id': 2, 'type': 'camera/stream', 'entity_id': entity, 'format': 'hls'})
            hls = await socket.receive_json()
            hls_result = {'success': hls['success']}
            if not hls['success']:
                hls_result['error_code'] = hls['error']['code']
        await request('POST', '/api/services/camera/turn_off', headers=headers, json={'entity_id': entity})
        async with session.get(BASE + '/api/camera_proxy/' + entity, headers=headers) as response:
            off_status = response.status
            assert off_status == 503
        await request('POST', '/api/services/camera/turn_on', headers=headers, json={'entity_id': entity})
        async with session.get(BASE + '/api/camera_proxy/' + entity, headers=headers) as response:
            assert response.status == 200
            restored = await frame_metadata(await bounded(response.content), response.headers['Content-Type'])
        result = {'ha_version': config['version'], 'fixture': 'official demo camera with generated local checker patterns',
                  'snapshot': snapshot, 'mjpeg_content_type': stream_type, 'mjpeg_frames': frames,
                  'advancing_frame_hashes': True, 'mjpeg_wire_delimiter': first_delimiter,
                  'initial_standard_multipart_parser': 'timeout: HA boundary declaration includes delimiter dashes', 'unauthenticated_status': denied, 'camera_off_status': off_status,
                  'camera_on_restored': restored, 'capabilities': capabilities['result'], 'hls': hls_result,
                  'frame_byte_limit': limit, 'scope': 'isolated authenticated HA camera bytes only; no app/cloud/hardware proof'}
        Path('/config/camera-api-result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


asyncio.run(main())
