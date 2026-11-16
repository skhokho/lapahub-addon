"""Allowlisted, read-only evidence for HA wrapper presentation.

This module never chooses an entity by its name, parent device, state or suffix.
Only an exact switch_as_x registry option can establish a relationship. The
result is presentation evidence only; it cannot authorize or retarget controls.
"""
import asyncio
import re

_ENTITY = re.compile(r"[a-z_]+\.[a-z0-9_]+\Z")
_REGISTRY_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
MAX_REGISTRY_ENTRIES = 20_000
MAX_PRESENTATION_ENTRIES = 1_000
REGISTRY_READ_TIMEOUT = 15


def _entity_id(value):
    return isinstance(value, str) and len(value) <= 200 and _ENTITY.fullmatch(value) is not None


def _registry_id(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and _REGISTRY_ID.fullmatch(value) is not None


def unavailable_registry():
    """A fresh negative observation, never a cached last-good graph."""
    return {"complete": False, "entries": []}


def normalize_registry(entries):
    """Return only validated candidate pairs from a complete registry/list result.

    Malformed identity rows or duplicate identities invalidate the snapshot:
    otherwise a malformed second row could disguise a competing wrapper. Invalid
    relations are omitted, and every wrapper pointing to a competing source is
    omitted even when one of those wrappers is inverted or malformed.
    """
    if not isinstance(entries, list) or len(entries) > MAX_REGISTRY_ENTRIES:
        return unavailable_registry()

    by_entity = {}
    registry_ids = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return unavailable_registry()
        entity_id, registry_id = entry.get("entity_id"), entry.get("id")
        if not _entity_id(entity_id) or not _registry_id(registry_id):
            return unavailable_registry()
        if entity_id in by_entity or registry_id in registry_ids:
            return unavailable_registry()
        by_entity[entity_id] = entry
        registry_ids.add(registry_id)

    # Count all declared switch_as_x references, including ineligible wrappers,
    # so an inverted/disabled competing alias cannot make a source look unique.
    source_users = {}
    for entry in entries:
        if entry.get("platform") != "switch_as_x":
            continue
        options = entry.get("options")
        options = options.get("switch_as_x") if isinstance(options, dict) else None
        source_id = options.get("entity_id") if isinstance(options, dict) else None
        if _entity_id(source_id):
            source_users.setdefault(source_id, []).append(entry["entity_id"])

    normalized = {}
    for entry in entries:
        if entry.get("platform") != "switch_as_x" or entry.get("disabled_by") is not None:
            continue
        options = entry.get("options")
        options = options.get("switch_as_x") if isinstance(options, dict) else None
        if not isinstance(options, dict) or options.get("invert") is not False:
            continue
        entity_id = entry["entity_id"]
        if entity_id.split(".", 1)[0] not in {"light", "fan", "cover", "valve", "siren", "lock"}:
            continue
        source_id = options.get("entity_id")
        if not _entity_id(source_id) or not source_id.startswith("switch.") or source_id == entity_id:
            continue
        source = by_entity.get(source_id)
        if source is None or source.get("platform") == "switch_as_x" or source.get("disabled_by") is not None:
            continue
        if len(source_users.get(source_id, [])) != 1:
            continue
        # Source links into other wrappers or chains are not folded by v1.
        if entity_id in source_users:
            continue
        normalized[entity_id] = {
            "entityId": entity_id,
            "registryId": entry["id"],
            "wrapper": {
                "integration": "switch_as_x",
                "sourceEntityId": source_id,
                "sourceRegistryId": source["id"],
                "invert": False,
            },
        }
        normalized[source_id] = {"entityId": source_id, "registryId": source["id"]}
    if len(normalized) > MAX_PRESENTATION_ENTRIES:
        return unavailable_registry()
    return {"complete": True, "entries": [normalized[key] for key in sorted(normalized)]}


async def collect_registry_presentation(session, base_url, supervisor_token):
    """Acquire one bounded authenticated registry response with no service calls.

    Negative responses, malformed replies and transport failures explicitly
    invalidate presentation evidence. Cancellation propagates for shutdown.
    No received payload, credential, URL or exception text is logged here.
    """
    async def read():
        ws_url = base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/api/websocket"
        async with session.ws_connect(ws_url, max_msg_size=8 * 1024 * 1024) as ws:
            message = await ws.receive_json()
            if not isinstance(message, dict) or message.get("type") != "auth_required":
                return unavailable_registry()
            await ws.send_json({"type": "auth", "access_token": supervisor_token})
            message = await ws.receive_json()
            if not isinstance(message, dict) or message.get("type") != "auth_ok":
                return unavailable_registry()
            await ws.send_json({"id": 1, "type": "config/entity_registry/list"})
            message = await ws.receive_json()
            if (not isinstance(message, dict) or message.get("id") != 1 or message.get("type") != "result"
                    or message.get("success") is not True or "result" not in message):
                return unavailable_registry()
            return normalize_registry(message["result"])

    try:
        return await asyncio.wait_for(read(), timeout=REGISTRY_READ_TIMEOUT)
    except Exception:
        return unavailable_registry()
