"""Three HA 2026.10.4 demo climate/lock/media protocol cases; run only in ephemeral container.

No cloud client or home credentials are used. /fixture contains reviewed addon
source. This checks service validation and WS state, never physical behavior.
"""
import ast
import asyncio
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
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
        for _ in range(60):
            states = await request('GET', '/api/states', headers=headers)
            domains = {s['entity_id'].split('.')[0] for s in states}
            if {'climate', 'lock', 'media_player'} <= domains:
                break
            await asyncio.sleep(1)
        assert {'climate', 'lock', 'media_player'} <= domains
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
            results = []
            journal_path = Path('/config/protocol-journal.sqlite3')
            for domain in ('climate', 'lock', 'media_player'):
                states = await request('GET', '/api/states', headers=headers)
                candidates = [s for s in states if s['entity_id'].startswith(domain + '.')]
                if domain == 'climate':
                    target = next(s for s in candidates if isinstance(s['attributes'].get('temperature'), (int, float)))
                    attrs = target['attributes']
                    previous = attrs['temperature']
                    expected = previous + 1 if previous + 1 <= attrs.get('max_temp', 35) else previous - 1
                    assert attrs.get('min_temp', 7) <= expected <= attrs.get('max_temp', 35)
                    action, params, attribute = 'set_temperature', {'temperature': expected}, 'temperature'
                elif domain == 'lock':
                    target = next(s for s in candidates if s['state'] in ('locked', 'unlocked'))
                    previous = target['state']
                    expected = 'unlocked' if previous == 'locked' else 'locked'
                    action, params, attribute = ('unlock' if previous == 'locked' else 'lock'), {}, None
                else:
                    target = next(s for s in candidates if isinstance(s['attributes'].get('volume_level'), (int, float)))
                    previous = target['attributes']['volume_level']
                    expected = 0.37 if previous != 0.37 else 0.63
                    action, params, attribute = 'volume_set', {'volume_level': expected}, 'volume_level'
                assert previous != expected
                command_id = 'isolated-' + domain + '-' + uuid.uuid4().hex
                actual.command_journal = CommandJournal(journal_path)
                dispatched = datetime.now(timezone.utc)
                record = {'id': command_id, 'domain': domain, 'entity_id': target['entity_id'],
                          'action': action, 'params': params, 'before': previous, 'expected': expected}
                try:
                    await actual.execute_command({'id': command_id, 'entity_id': target['entity_id'],
                        'action': action, 'params': params, 'expires_at': time.time() + 60})
                    row = actual.command_journal.db.execute('SELECT fingerprint,outcome,trace FROM commands WHERE id=?', (command_id,)).fetchone()
                    record.update(fingerprint=row[0], receipt=row[1], trace=json.loads(row[2]))
                    assert (command_id, 'service_accepted') in actual.command_journal.pending()
                    async def confirmed():
                        while True:
                            msg = await socket.receive_json()
                            if msg.get('type') != 'event':
                                continue
                            event = msg['event']
                            data = event['data']
                            state = data.get('new_state') or {}
                            observed = state.get('attributes', {}).get(attribute) if attribute else state.get('state')
                            if (data['entity_id'] == target['entity_id'] and observed == expected and
                                datetime.fromisoformat(event['time_fired']) >= dispatched):
                                return event
                    event = await asyncio.wait_for(confirmed(), 30)
                    record.update(ws_event_time=event['time_fired'], ws_observed=expected, result='pass')
                except Exception as error:
                    record.update(result='fail', error_type=type(error).__name__)
                    Path('/config/' + command_id + '.json').write_text(json.dumps(record, indent=2))
                    raise
                finally:
                    actual.command_journal.close()
                reopened = CommandJournal(journal_path)
                try:
                    assert (command_id, 'service_accepted') in reopened.pending()
                    record['receipt_reopened'] = True
                finally:
                    reopened.close()
                Path('/config/' + command_id + '.json').write_text(json.dumps(record, indent=2))
                results.append(record)
        result = {'ha_version': config['version'], 'fixture': 'official local demo', 'cases': results,
                  'scope': 'HA service/state protocol only; no hardware, native UI, ownership or cloud routing',
                  'camera': 'skipped: application preview is a placeholder; no implemented stream/snapshot client path found'}
        Path('/config/protocol-results.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


asyncio.run(main())
