"""Bounded, lease-driven camera JPEG capture. No frames or leases are persisted."""

import asyncio
from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import quote
import aiohttp


def safe_camera_attributes(attributes):
    """Only UI metadata may leave HA; camera tokens and URLs are never copied."""
    if not isinstance(attributes, dict):
        return {}
    result = {}
    name = attributes.get('friendly_name')
    if isinstance(name, str):
        result['friendly_name'] = name[:200]
    features = attributes.get('supported_features')
    if isinstance(features, int) and not isinstance(features, bool) and 0 <= features <= 2**31 - 1:
        result['supported_features'] = features
    motion = attributes.get('motion_detection')
    if isinstance(motion, bool):
        result['motion_detection'] = motion
    return result


def safe_camera_state(value):
    return value if value in ('idle', 'streaming', 'recording', 'off', 'unavailable', 'unknown') else 'unknown'

MAX_FRAME_BYTES = 512 * 1024
MAX_WIDTH = 1920
MAX_HEIGHT = 1080


class CameraFrameError(ValueError):
    """A frame is unavailable or violates the bounded JPEG contract."""


def validate_jpeg(data, content_type):
    """Check bounded JPEG structure and SOF dimensions, without native decoding."""
    if (not isinstance(data, bytes) or not 0 < len(data) <= MAX_FRAME_BYTES or
            (content_type if isinstance(content_type, str) else '').split(';', 1)[0].strip().lower() not in ('image/jpeg', 'image/jpg') or
            not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9')):
        raise CameraFrameError('Invalid bounded JPEG')
    position, segments, dimensions, scanned = 2, 0, None, False
    sof = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
    while position < len(data) and segments < 1024:
        if data[position] != 0xff:
            raise CameraFrameError('Invalid JPEG marker')
        while position < len(data) and data[position] == 0xff:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        segments += 1
        if marker == 0xd9:
            if dimensions and scanned and position == len(data):
                return dimensions
            break
        if marker in (0, 0xd8) or 0xd0 <= marker <= 0xd7:
            break
        if marker == 1:
            continue
        if position + 2 > len(data):
            break
        length = int.from_bytes(data[position:position + 2], 'big')
        if length < 2 or position + length > len(data) - 2:
            break
        if marker in sof:
            if dimensions is not None or length < 11:
                break
            height = int.from_bytes(data[position + 3:position + 5], 'big')
            width = int.from_bytes(data[position + 5:position + 7], 'big')
            components = data[position + 7]
            if (not 1 <= components <= 4 or length != 8 + 3 * components or
                    not 0 < width <= MAX_WIDTH or not 0 < height <= MAX_HEIGHT or
                    width * height > 2073600):
                break
            dimensions = width, height
        if marker == 0xda:
            if not dimensions or length < 8:
                break
            components = data[position + 2]
            if not 1 <= components <= 4 or length != 6 + 2 * components:
                break
            scanned = True
            position += length
            while position < len(data):
                if data[position] != 0xff:
                    position += 1
                    continue
                start = position
                while position < len(data) and data[position] == 0xff:
                    position += 1
                if position >= len(data):
                    break
                if data[position] == 0 or 0xd0 <= data[position] <= 0xd7:
                    position += 1
                    continue
                position = start
                break
        else:
            position += length
    raise CameraFrameError('Invalid JPEG structure or dimensions')


class CameraCaptureWorker:
    """Outbound-only RAM metadata worker; no control calls, frame cache or DBs."""
    def __init__(self, session, ha_base_url, ha_token, relay_url, hub_id, hub_token,
                 running=lambda: True, now=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.session = session
        self.ha_base = ha_base_url.rstrip('/')
        self.ha_token = ha_token
        self.relay = relay_url.rstrip('/')
        self.hub_id = hub_id
        self.hub_token = hub_token
        self.running = running
        self.now, self.monotonic = now, monotonic
        self.semaphore = asyncio.Semaphore(2)
        self.sequences, self.attempts, self.blocked = {}, {}, set()
        self.statuses = {}
        self.bindings = {}

    def headers(self):
        token = self.hub_token()
        return {'Authorization': 'Bearer ' + token} if isinstance(token, str) and token else None

    async def run(self):
        try:
            while self.running():
                try:
                    delay = await self.step()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Fail closed without logging URLs, tokens, bodies or frames.
                    delay = 2
                await asyncio.sleep(delay)
        finally:
            self.sequences.clear()
            self.attempts.clear()
            self.statuses.clear()
            self.blocked.clear()
            self.bindings.clear()

    async def small_json(self, response):
        body = bytearray()
        async for chunk in response.content.iter_chunked(4096):
            if len(body) + len(chunk) > 16384:
                raise ValueError('Relay metadata exceeds limit')
            body.extend(chunk)
        return json.loads(body)

    def parse_views(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('views'), list) or len(payload['views']) > 4:
            raise ValueError('Invalid camera demand')
        views = {}
        for item in payload['views']:
            if not isinstance(item, dict):
                raise ValueError('Invalid camera view')
            view_id, entity = item.get('viewId'), item.get('entityId')
            sequence = item.get('lastSequence')
            if (not isinstance(view_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', view_id) or
                    not isinstance(entity, str) or not re.fullmatch(r'camera\.[a-z0-9_]{1,128}', entity) or
                    isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence < 2**53 - 1):
                raise ValueError('Invalid camera binding')
            if view_id in self.bindings and self.bindings[view_id] != entity:
                raise ValueError('Camera view binding changed')
            expires = item.get('expiresAt')
            if not isinstance(expires, str):
                raise ValueError('Invalid camera expiry')
            expiry = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if expiry.tzinfo is None or expiry.utcoffset() is None:
                raise ValueError('Camera expiry requires timezone')
            remaining = (expiry - self.now()).total_seconds()
            if remaining <= 0:
                continue
            if remaining > 65:
                raise ValueError('Camera lease exceeds contract')
            interval = item.get('minCaptureIntervalMs')
            if (isinstance(interval, bool) or not isinstance(interval, int) or not 500 <= interval <= 60000 or
                    item.get('maxFrameBytes') != MAX_FRAME_BYTES or item.get('requestedWidth') != 640 or
                    item.get('requestedHeight') != 360 or view_id in views):
                raise ValueError('Invalid capture bounds')
            views[view_id] = {'entityId': entity, 'lastSequence': sequence,
                              'minCaptureIntervalMs': interval,
                              'deadline': self.monotonic() + min(remaining, 60)}
        return views

    async def step(self):
        headers = self.headers()
        if not headers:
            return 2
        try:
            async with self.session.post(self.relay + '/hub/demand', json={'hubId': self.hub_id},
                                         headers=headers, allow_redirects=False,
                                         timeout=aiohttp.ClientTimeout(total=3)) as response:
                if response.status != 200:
                    if response.status in (403, 404, 410):
                        self.blocked.update(self.bindings)
                    return 2
                views = self.parse_views(await self.small_json(response))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
            # No capture from a remembered lease after failed reauthorization.
            return 2
        live_ids = set(views)
        self.blocked.intersection_update(live_ids)
        self.bindings = {key: view['entityId'] for key, view in views.items()}
        self.sequences = {key: value for key, value in self.sequences.items() if key in live_ids}
        self.attempts = {key: value for key, value in self.attempts.items() if key in live_ids}
        self.statuses = {key: value for key, value in self.statuses.items() if key in live_ids}
        await asyncio.gather(*(self.capture(view_id, view) for view_id, view in views.items()))
        return 1 if views else 2

    async def status(self, view_id, state):
        self.blocked.add(view_id)
        self.statuses[view_id] = state
        headers = self.headers()
        if not headers:
            return
        try:
            async with self.session.post(self.relay + '/hub/status', json={
                    'hubId': self.hub_id, 'viewId': view_id, 'state': state}, headers=headers,
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=2)) as response:
                if response.status == 200:
                    self.statuses.pop(view_id, None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass

    async def capture(self, view_id, view):
        if view_id in self.blocked:
            if view_id in self.statuses:
                await self.status(view_id, self.statuses[view_id])
            return
        if self.monotonic() - self.attempts.get(view_id, float('-inf')) < view['minCaptureIntervalMs'] / 1000:
            return
        async with self.semaphore:
            if not self.running() or self.monotonic() >= view['deadline']:
                return
            self.attempts[view_id] = self.monotonic()
            data = bytearray()
            stage = 'ha'
            try:
                url = self.ha_base + '/api/camera_proxy/' + quote(view['entityId'], safe='')
                async with self.session.get(url, params={'width': 640, 'height': 360},
                        headers={'Authorization': 'Bearer ' + self.ha_token}, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=min(3, max(.001, view['deadline'] - self.monotonic())))) as response:
                    if response.status != 200:
                        await self.status(view_id, 'off' if response.status == 503 else 'unavailable')
                        return
                    if response.content_length is not None and response.content_length > MAX_FRAME_BYTES:
                        await self.status(view_id, 'error')
                        return
                    mime = response.headers.get('Content-Type', '')
                    async for chunk in response.content.iter_chunked(16384):
                        if len(data) + len(chunk) > MAX_FRAME_BYTES:
                            raise CameraFrameError('Camera frame exceeds limit')
                        data.extend(chunk)
                observed = self.now().astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                validate_jpeg(bytes(data), mime)
                if not self.running() or self.monotonic() >= view['deadline']:
                    return
                headers = self.headers()
                if not headers:
                    return
                sequence = max(self.sequences.get(view_id, 0), view['lastSequence']) + 1
                if sequence >= 2**53:
                    await self.status(view_id, 'error')
                    return
                self.sequences[view_id] = sequence
                headers.update({'Content-Type': 'image/jpeg', 'X-Lapa-Frame-Sequence': str(sequence),
                                'X-Lapa-Observed-At': observed})
                stage = 'relay'
                async with self.session.post(self.relay + '/hub/frame', params={
                        'hubId': self.hub_id, 'viewId': view_id}, data=data, headers=headers,
                        allow_redirects=False, timeout=aiohttp.ClientTimeout(
                            total=min(3, max(.001, view['deadline'] - self.monotonic())))) as response:
                    if response.status in (403, 404, 410, 503):
                        self.blocked.add(view_id)
                    elif response.status in (413, 415, 422):
                        await self.status(view_id, 'error')
                    # 401 pauses until ordinary token refresh; 409 reconciles next
                    # demand.lastSequence; 429 backs off at the capture interval.
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # An unavailable HA source needs a new viewer intent; relay
                # transport loss pauses until reauthorization, never replaying bytes.
                if stage == 'ha':
                    await self.status(view_id, 'unavailable')
            except CameraFrameError:
                await self.status(view_id, 'error')
            finally:
                data.clear()
