"""Read-only exact-entity preflight. No cache, name matching or target rebinding."""
import asyncio
import json
import re
import time
from urllib.parse import quote
import aiohttp
from reliability import check_command_deadline

MAX_STATE_BYTES = 65536
PREFLIGHT_SECONDS = 1.0


class TargetNotDispatched(RuntimeError):
    """A target could not be proved usable; no physical service POST occurred."""


def reject(reason):
    raise TargetNotDispatched(reason)


async def require_live_target(session, base_url, token, entity_id, domain, expires_at=None):
    check_command_deadline(expires_at)
    if (not isinstance(entity_id, str) or len(entity_id) > 200 or
            not re.fullmatch(r'[a-z_]+\.[a-z0-9_]+', entity_id) or
            domain not in (entity_id.split('.')[0], 'homeassistant')):
        reject('invalid_target')
    remaining = PREFLIGHT_SECONDS if expires_at is None else min(PREFLIGHT_SECONDS, float(expires_at) - time.time())
    check_command_deadline(expires_at)
    try:
        url = base_url.rstrip('/') + '/api/states/' + quote(entity_id, safe='')
        async with session.get(url, headers={'Authorization': 'Bearer ' + (token or '')},
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=max(.001, remaining))) as response:
            if response.status == 404:
                reject('missing_target')
            if response.status != 200:
                reject('lookup_unavailable')
            if response.content_type != 'application/json':
                reject('invalid_target_response')
            if response.content_length is not None and response.content_length > MAX_STATE_BYTES:
                reject('invalid_target_response')
            body = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                if len(body) + len(chunk) > MAX_STATE_BYTES:
                    reject('invalid_target_response')
                body.extend(chunk)
            state = json.loads(body)
            if (not isinstance(state, dict) or state.get('entity_id') != entity_id or
                    not isinstance(state.get('state'), str) or not state['state'] or
                    not isinstance(state.get('attributes'), dict)):
                reject('invalid_target_response')
            # Unknown can be legitimate for stateless buttons/remotes. Missing or
            # explicitly unavailable/restored targets cannot prove a service target.
            if state['state'] == 'unavailable' or state['attributes'].get('restored') is True:
                reject('unavailable_target')
    except asyncio.CancelledError:
        raise
    except TargetNotDispatched:
        check_command_deadline(expires_at)
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        check_command_deadline(expires_at)
        raise TargetNotDispatched('lookup_uncertain') from None
    check_command_deadline(expires_at)
