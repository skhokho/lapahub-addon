"""One HA 2026.10.4 demo-cover protocol case; run only in ephemeral container.

No cloud client or home credentials are used. /fixture contains reviewed addon
source. This checks service validation and WS state, never physical behavior.
"""
import ast
import asyncio
import json
import logging
from pathlib import Path
import secrets
import sys
import time
import uuid

import aiohttp

sys.path.insert(0, '/fixture')
from reliability import CommandJournal, CommandExpired, check_command_deadline

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
        states = []
        for _ in range(60):
            states = await request('GET', '/api/states', headers=headers)
            covers = [s for s in states if s['entity_id'].startswith('cover.') and s['attributes'].get('current_position') is not None]
            if covers:
                break
            await asyncio.sleep(1)
        assert covers, 'Official demo cover not loaded'
        target = covers[0]
        # Official demo rounds positions to tens; use a supported fixture target.
        position = 40 if target['attributes']['current_position'] != 40 else 60
        async with session.ws_connect(BASE + '/api/websocket') as socket:
            assert (await socket.receive_json())['type'] == 'auth_required'
            await socket.send_json({'type': 'auth', 'access_token': token['access_token']})
            assert (await socket.receive_json())['type'] == 'auth_ok'
            await socket.send_json({'id': 1, 'type': 'subscribe_events', 'event_type': 'state_changed'})
            assert (await socket.receive_json())['success']
            tree = ast.parse(Path('/fixture/run.py').read_text())
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LapaHubAddon')
            names = {'execute_command', '_execute_command_once', 'call_ha_service'}
            namespace = dict(asyncio=asyncio, logger=logging.getLogger('protocol'), aiohttp=aiohttp,
                             HA_BASE_URL=BASE, SUPERVISOR_TOKEN=token['access_token'],
                             CommandExpired=CommandExpired, check_command_deadline=check_command_deadline)
            for method in cls.body:
                if isinstance(method, ast.AsyncFunctionDef) and method.name in names:
                    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), '/fixture/run.py', 'exec'), namespace)
            actual = type('ActualAddonExecutor', (), {name: namespace[name] for name in names})()
            actual.session = session
            command_id = 'isolated-cover-' + uuid.uuid4().hex
            journal_path = Path('/config/protocol-journal.sqlite3')
            actual.command_journal = CommandJournal(journal_path)
            try:
                await actual.execute_command({'id': command_id, 'entity_id': target['entity_id'],
                    'action': 'set_cover_position', 'params': {'position': position}, 'expires_at': time.time() + 60})
                assert (command_id, 'service_accepted') in actual.command_journal.pending()
                row = actual.command_journal.db.execute('SELECT fingerprint,outcome FROM commands WHERE id=?', (command_id,)).fetchone()
                Path('/config/' + command_id + '.json').write_text(json.dumps({'id': command_id, 'position': position, 'fingerprint': row[0], 'receipt': row[1]}))
                async def confirmed():
                    while True:
                        msg = await socket.receive_json()
                        if msg.get('type') != 'event':
                            continue
                        data = msg['event']['data']
                        state = data.get('new_state') or {}
                        if data['entity_id'] == target['entity_id'] and state.get('attributes', {}).get('current_position') == position:
                            return state
                observed = await asyncio.wait_for(confirmed(), 30)
            finally:
                actual.command_journal.close()
            reopened = CommandJournal(journal_path)
            try:
                assert (command_id, 'service_accepted') in reopened.pending()
            finally:
                reopened.close()
        print(json.dumps({'ha_version': config['version'], 'fixture': 'official demo cover',
            'action': 'set_cover_position', 'position': position, 'receipt': 'service_accepted',
            'ws_position': observed['attributes']['current_position'], 'command_id': command_id,
            'receipt_reopened': True, 'result': 'pass',
            'scope': 'isolated HA service and state-event protocol only; no hardware or cloud routing'}))


asyncio.run(main())
