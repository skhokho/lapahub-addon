"""Actual addon handlers against fake HA HTTP; no physical-device certification."""
import ast
import asyncio
from datetime import datetime, timezone
import logging
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest
import aiohttp
from aiohttp import web
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lapahub/rootfs/app'))
from reliability import CommandJournal,CommandExpired,check_command_deadline

# Explicit canonical services currently admitted by the backend. Empty payloads
# exercise routing; concrete service fields below exercise typed parameter forwarding.
CASES={
 'light': {'turn_on':{'brightness':128},'turn_off':{},'toggle':{}},
 'switch': {'turn_on':{},'turn_off':{},'toggle':{}},
 'input_boolean': {'turn_on':{},'turn_off':{},'toggle':{}},
 'siren': {'turn_on':{},'turn_off':{}},
 'climate': {'turn_on':{},'turn_off':{},'set_temperature':{'temperature':22.5},'set_hvac_mode':{'hvac_mode':'heat'},
             'set_fan_mode':{'fan_mode':'auto'},'set_preset_mode':{'preset_mode':'eco'},'set_swing_mode':{'swing_mode':'vertical'}},
 'cover': {'open':{},'close':{},'open_cover':{},'close_cover':{},'stop_cover':{},'set_cover_position':{'position':37},
           'open_cover_tilt':{},'close_cover_tilt':{},'stop_cover_tilt':{},'set_cover_tilt_position':{'tilt_position':42}},
 'fan': {'turn_on':{},'turn_off':{},'toggle':{},'set_percentage':{'percentage':42},'set_preset_mode':{'preset_mode':'eco'},
         'oscillate':{'oscillating':True},'set_direction':{'direction':'reverse'}},
 'lock': {'lock':{},'unlock':{}},
 'media_player': {'turn_on':{},'turn_off':{},'media_play_pause':{},'media_play':{},'media_pause':{},'media_stop':{},
                  'media_next_track':{},'media_previous_track':{},'volume_set':{'volume_level':.37},
                  'volume_mute':{'is_volume_muted':True},'select_source':{'source':'Fixture'},
                  'play_media':{'media_content_id':'fixture-local','media_content_type':'music'}},
 'remote': {'turn_on':{},'turn_off':{},'send_command':{'command':'fixture-button'}},
 'button': {'press':{}},
 'vacuum': {'start':{},'pause':{},'stop':{},'return_to_base':{},'locate':{},'set_fan_speed':{'fan_speed':'quiet'}},
 'water_heater': {'turn_on':{},'turn_off':{},'set_temperature':{'temperature':45},'set_operation_mode':{'operation_mode':'eco'}},
 'humidifier': {'turn_on':{},'turn_off':{},'set_humidity':{'humidity':45},'set_mode':{'mode':'normal'}},
 'valve': {'open_valve':{},'close_valve':{},'stop_valve':{},'set_valve_position':{'position':42}},
}


def actual_type(base):
 source=Path(__file__).resolve().parents[1]/'lapahub/rootfs/app/run.py'
 cls=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='LapaHubAddon')
 names={'execute_command','_execute_command_once','call_ha_service','report_command_result','handle_state_change','sync_devices','push_state_change_to_cloud','push_devices_to_cloud','trigger_scene'}
 ns=dict(asyncio=asyncio,datetime=datetime,timezone=timezone,aiohttp=aiohttp,logger=logging.getLogger('device-matrix'),
         CommandExpired=CommandExpired,check_command_deadline=check_command_deadline,HA_BASE_URL=base,SUPERVISOR_TOKEN='synthetic-ha')
 methods=[m for m in cls.body if isinstance(m,ast.AsyncFunctionDef) and m.name in names]
 for method in methods:exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),str(source),'exec'),ns)
 return type('ActualDeviceHandlers',(),{m.name:ns[m.name] for m in methods})


class ContractCoverageTests(unittest.TestCase):
 def test_matrix_matches_every_existing_non_camera_backend_action(self):
  contract=json.loads((Path(__file__).parent/'fixtures/backend_device_control_contract.json').read_text())
  contract.pop('camera')  # Camera viewing/control privacy is a separately gated lane.
  self.assertEqual(set(CASES),set(contract))
  for domain,services in contract.items():self.assertEqual(set(CASES[domain]),set(services))


class ServiceMatrixTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.requests=[];self.acks=[];self.lookup_calls=[];self.lookup_status=200;self.lookup_mode='valid';self.post_gate=None;self.post_entered=asyncio.Event();self.lookup_gate=None;self.lookup_block_entity=None;self.redirects=0;self.flow=[];self.lookup_entered=asyncio.Event();self.status=200;self.cloud_states=[];self.cloud_status=200;self.ack_status=200
  async def lookup(request):
   self.assertEqual(request.headers['Authorization'],'Bearer synthetic-ha')
   entity=request.match_info['entity'];self.lookup_calls.append(entity);self.flow.append('get:'+entity);self.lookup_entered.set()
   if self.lookup_gate and self.lookup_block_entity in (None,entity):await self.lookup_gate.wait()
   if self.lookup_mode=='redirect':raise web.HTTPFound('/redirect-target')
   if self.lookup_mode=='invalid_shape':return web.json_response([])
   if self.lookup_mode=='wrong_mime':return web.Response(text='{}',content_type='text/html')
   if self.lookup_mode=='chunked_oversize':
    response=web.StreamResponse(headers={'Content-Type':'application/json'});await response.prepare(request)
    try:await response.write(b'x'*65537)
    except ConnectionResetError:pass
    return response
   if self.lookup_mode=='invalid_json':return web.Response(text='{invalid',content_type='application/json')
   if self.lookup_mode=='too_large':return web.json_response({'entity_id':entity,'state':'on','attributes':{'data':'x'*65536}})
   if self.lookup_mode=='wrong_entity':entity='switch.some_other_device'
   state='unknown' if self.lookup_mode=='unknown' else 'unavailable' if self.lookup_mode=='unavailable' else 'on'
   return web.json_response({'entity_id':entity,'state':state,'attributes':{'restored':self.lookup_mode=='restored'}},status=self.lookup_status)
  async def redirected(request):
   self.redirects+=1;return web.json_response({})
  async def service(request):
   self.assertEqual(request.headers['Authorization'],'Bearer synthetic-ha')
   payload=await request.json();self.flow.append('post:'+payload.get('entity_id','new-scene'));self.requests.append((request.match_info['domain'],request.match_info['service'],payload))
   self.post_entered.set()
   if self.post_gate:await self.post_gate.wait()
   return web.json_response([],status=self.status)
  async def acknowledgement(request):
   self.assertEqual(request.headers['Authorization'],'Bearer synthetic-cloud')
   self.acks.append(await request.json());return web.json_response({},status=self.ack_status)
  async def cloud_state(request):
   self.assertEqual(request.headers['Authorization'],'Bearer synthetic-cloud')
   self.cloud_states.append(await request.json());return web.json_response({},status=self.cloud_status)
  app=web.Application();app.router.add_get('/api/states/{entity}',lookup);app.router.add_get('/redirect-target',redirected);app.router.add_post('/cloud/updateDeviceState',cloud_state);app.router.add_post('/api/services/{domain}/{service}',service);app.router.add_post('/cloud/reportCommandResult',acknowledgement)
  self.runner=web.AppRunner(app);await self.runner.setup();site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
  self.base='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1]);self.session=aiohttp.ClientSession(trust_env=False)
  class Gateway:
   def get(inner,url,**kwargs):return self.session.get(url,**kwargs)
   def post(inner,url,**kwargs):
    if url.startswith('https://us-central1-'):url=self.base+'/cloud/'+url.rsplit('/',1)[-1]
    return self.session.post(url,**kwargs)
  self.tmp=tempfile.TemporaryDirectory();self.addon=actual_type(self.base)()
  self.addon.session=Gateway();self.addon.command_journal=CommandJournal(Path(self.tmp.name)/'journal.sqlite3')
  self.addon.firebase_credentials={'token':'synthetic-cloud'};self.addon.firebase_project='synthetic';self.addon.hub_id='synthetic'
  self.addon.devices={};self.addon.sensor_realtime=False;self.addon.state_publisher=SimpleNamespace(offer=Mock(),dropped=0)
  self.addon.entity_device_map={};self.addon.physical_devices={};self.addon.push_devices_to_cloud=AsyncMock()

 async def asyncTearDown(self):
  if self.lookup_gate:self.lookup_gate.set()
  if self.post_gate:self.post_gate.set()
  await self.session.close();await self.runner.cleanup();self.addon.command_journal.close();self.tmp.cleanup()

 async def test_every_admitted_non_camera_service_exact_http_and_ack(self):
  index=0
  for domain,services in CASES.items():
   for action,params in services.items():
    with self.subTest(domain=domain,action=action):
     index+=1;flow_before=len(self.flow);command_id='matrix-'+str(index);entity=domain+'.fixture'
     await self.addon.execute_command({'id':command_id,'entity_id':entity,'action':action,'params':params})
     expected_service={'open':'open_cover','close':'close_cover'}.get(action,action)
     self.assertEqual(self.requests[-1],(domain,expected_service,{**params,'entity_id':entity}))
     self.assertEqual(self.flow[flow_before:],['get:'+entity,'post:'+entity])
     self.assertIn((command_id,'service_accepted'),self.addon.command_journal.pending())
     await self.addon.report_command_result(command_id,'service_accepted')
     self.assertEqual(self.acks[-1]['commandId'],command_id);self.assertTrue(self.acks[-1]['success'])
     self.assertEqual(self.acks[-1]['outcome'],'service_accepted')
     self.addon.command_journal.acknowledge(command_id)
  self.assertEqual(len(self.requests),sum(map(len,CASES.values())))

 async def test_pool_name_uses_actual_switch_or_fan_entity_domain(self):
  for domain in ('switch','fan'):
   await self.addon.execute_command({'id':'pool-'+domain,'entity_id':domain+'.pool_pump',
       'domain':'light','action':'turn_on','params':{}})
   self.assertEqual(self.requests[-1],(domain,'turn_on',{'entity_id':domain+'.pool_pump'}))

 async def test_ha_rejections_never_acknowledge_device_service_acceptance(self):
  for status in (400,401,404,500,503):
   self.status=status;command_id='rejected-'+str(status)
   await self.addon.execute_command({'id':command_id,'entity_id':'switch.fixture','action':'turn_on','params':{}})
   self.assertIn((command_id,'unknown'),self.addon.command_journal.pending())
   await self.addon.report_command_result(command_id,'unknown')
   self.assertFalse(self.acks[-1]['success']);self.assertEqual(self.acks[-1]['outcome'],'unknown')

 async def test_actual_realtime_reports_each_supported_domain_with_original_ha_change_clock(self):
  for domain in CASES:
   with self.subTest(domain=domain):
    self.addon.state_publisher.offer.reset_mock()
    state={'entity_id':domain+'.fixture','state':{'climate':'heat','cover':'opening','lock':'locked','media_player':'playing','button':'2026-11-04T00:00:00Z','vacuum':'cleaning','water_heater':'eco','valve':'open'}.get(domain,'on'),'last_updated':'2026-01-01T01:02:03.123456+00:00','attributes':{'friendly_name':'Fixture'}}
    await self.addon.handle_state_change({'entity_id':state['entity_id'],'new_state':state})
    self.addon.state_publisher.offer.assert_called_once()
    payload=self.addon.state_publisher.offer.call_args.args[0]
    self.assertEqual(payload['entity_id'],state['entity_id']);self.assertEqual(payload['last_updated'],state['last_updated'])
    self.assertEqual(payload['state'],state['state'])

 async def test_discovery_covers_admitted_domains_but_does_not_invent_native_pump_or_enable_camera(self):
  states=[{'entity_id':domain+'.fixture','state':'on','attributes':{}} for domain in CASES]
  states += [{'entity_id':'pump.native','state':'on','attributes':{}},{'entity_id':'camera.fixture','state':'streaming','attributes':{'access_token':'fixture-not-forwarded'}}]
  self.addon.get_ha_states=AsyncMock(return_value=states)
  await self.addon.sync_devices()
  devices=self.addon.push_devices_to_cloud.await_args.args[0]
  self.assertEqual({d['domain'] for d in devices},set(CASES))

 async def test_all_supported_realtime_payloads_reach_fake_cloud_with_original_clock(self):
  for domain in CASES:
   self.addon.state_publisher.offer.reset_mock()
   state={'entity_id':domain+'.fixture','state':'on','last_updated':'2026-01-01T00:00:00.123456Z','attributes':{}}
   await self.addon.handle_state_change({'entity_id':state['entity_id'],'new_state':state})
   device=self.addon.state_publisher.offer.call_args.args[0]
   self.assertTrue(await self.addon.push_state_change_to_cloud(device))
   self.assertEqual(self.cloud_states[-1]['device']['last_updated'],state['last_updated'])
   self.assertEqual(self.cloud_states[-1]['device']['entity_id'],state['entity_id'])

 async def test_cloud_state_failure_is_returned_for_publication_retry(self):
  self.cloud_status=503
  self.assertFalse(await self.addon.push_state_change_to_cloud({'entity_id':'water_heater.fixture',
      'state':'eco','last_updated':'2026-01-01T00:00:00Z','attributes':{}}))

 async def test_http_ack_failure_keeps_accepted_receipt_pending(self):
  command_id='ack-failure'
  await self.addon.execute_command({'id':command_id,'entity_id':'valve.fixture','action':'open_valve','params':{}})
  self.ack_status=503
  with self.assertRaisesRegex(RuntimeError,'503'):
   await self.addon.report_command_result(command_id,'service_accepted')
  self.assertIn((command_id,'service_accepted'),self.addon.command_journal.pending())

 async def test_preflight_known_missing_invalid_and_unavailable_never_posts(self):
  for index,(status,mode) in enumerate(((404,'valid'),(200,'wrong_entity'),(200,'invalid_json'),
        (200,'unavailable'),(200,'restored'),(200,'too_large'),(500,'valid'),(401,'valid'),
        (200,'redirect'),(200,'wrong_mime'),(200,'invalid_shape'),(200,'chunked_oversize'))):
   with self.subTest(status=status,mode=mode):
    self.lookup_status,self.lookup_mode=status,mode;before=len(self.requests);command_id='preflight-deny-'+str(index)
    await self.addon.execute_command({'id':command_id,'entity_id':'switch.fixture','action':'turn_on','params':{}})
    self.assertEqual(len(self.requests),before)
    self.assertIn((command_id,'failed'),self.addon.command_journal.pending())
    self.assertNotIn('addonDispatchedAt',self.addon.command_journal.trace(command_id))
    self.assertEqual(self.redirects,0)

 async def test_stateless_unknown_existing_target_can_dispatch(self):
  self.lookup_mode='unknown'
  await self.addon.execute_command({'id':'unknown-button','entity_id':'button.fixture','action':'press','params':{}})
  self.assertEqual(self.requests[-1],('button','press',{'entity_id':'button.fixture'}))

 async def test_deadline_expires_during_preflight_without_post_or_fake_dispatch_trace(self):
  import time
  self.lookup_gate=asyncio.Event()
  command={'id':'preflight-expired','entity_id':'switch.fixture','action':'turn_on','params':{},'expires_at':time.time()+.05}
  task=asyncio.create_task(self.addon.execute_command(command))
  await asyncio.sleep(.08);self.lookup_gate.set();await task
  self.assertEqual(self.requests,[])
  self.assertIn(('preflight-expired','expired'),self.addon.command_journal.pending())
  self.assertNotIn('addonDispatchedAt',self.addon.command_journal.trace('preflight-expired'))

 async def test_network_uncertain_preflight_is_not_dispatched_and_not_replayed(self):
  self.lookup_gate=asyncio.Event()
  command={'id':'preflight-timeout','entity_id':'switch.fixture','action':'turn_on','params':{}}
  await self.addon.execute_command(command)
  self.lookup_gate.set()
  self.assertEqual(self.requests,[])
  self.assertIn(('preflight-timeout','failed'),self.addon.command_journal.pending())
  self.assertNotIn('addonDispatchedAt',self.addon.command_journal.trace('preflight-timeout'))
  before=len(self.lookup_calls)
  await self.addon.execute_command(command)
  self.assertEqual(len(self.lookup_calls),before)
  self.assertEqual(self.requests,[])

 async def test_scene_unknown_and_existing_target_still_dispatches(self):
  self.lookup_mode='unknown'
  self.assertTrue(await self.addon.call_ha_service('scene','turn_on',{'entity_id':'scene.fixture'}))
  self.assertEqual(self.requests,[('scene','turn_on',{'entity_id':'scene.fixture'})])

 async def test_dispatch_timestamp_only_appears_after_live_preflight(self):
  import time
  self.lookup_gate=asyncio.Event()
  task=asyncio.create_task(self.addon.execute_command({'id':'precise-dispatch','entity_id':'switch.fixture','action':'turn_on','params':{}}))
  await self.lookup_entered.wait()
  self.assertNotIn('addonDispatchedAt',self.addon.command_journal.trace('precise-dispatch'))
  released=time.time();self.lookup_gate.set();await task
  self.assertGreaterEqual(self.addon.command_journal.trace('precise-dispatch')['addonDispatchedAt'],released)

 async def test_concurrent_intent_deadlines_do_not_leak_between_tasks(self):
  import time
  from reliability import active_command
  self.lookup_gate=asyncio.Event();self.lookup_block_entity='switch.first'
  # Advance only the wall clock under test, after the first real HTTP lookup
  # arrives. A loaded runner must not consume a 50ms setup window and strand
  # an unbounded event wait before any lookup can happen.
  clock=[time.time()]
  with patch('time.time',side_effect=lambda:clock[0]):
   first=asyncio.create_task(self.addon.execute_command({'id':'context-first','entity_id':'switch.first','action':'turn_on','params':{},'expires_at':clock[0]+30}))
   try:
    await asyncio.wait_for(self.lookup_entered.wait(),2)
    await asyncio.wait_for(self.addon.execute_command({'id':'context-second','entity_id':'switch.second','action':'turn_on','params':{},'expires_at':clock[0]+60}),2)
    clock[0]+=31
    self.lookup_gate.set()
    await asyncio.wait_for(first,2)
   finally:
    self.lookup_gate.set()
    if not first.done():first.cancel()
    await asyncio.gather(first,return_exceptions=True)
  self.assertEqual(self.requests,[('switch','turn_on',{'entity_id':'switch.second'})])
  self.assertIn(('context-first','expired'),self.addon.command_journal.pending())
  self.assertIn(('context-second','service_accepted'),self.addon.command_journal.pending())
  self.assertIsNone(active_command.get())

 async def test_concurrent_deadline_fixture_survives_delayed_scheduling(self):
  original=self.addon.execute_command
  async def delayed(command):
   if command['id']=='context-first':await asyncio.sleep(.12)
   return await original(command)
  # This delay exceeds the former 50ms deadline: the old test waited forever
  # for a lookup that the correctly expired command could never issue.
  with patch.object(self.addon,'execute_command',side_effect=delayed):
   await asyncio.wait_for(self.test_concurrent_intent_deadlines_do_not_leak_between_tasks(),5)

 async def test_preflight_failure_preserves_existing_receipt_and_scene_tombstone(self):
  import hashlib
  import sqlite3
  from managed_scenes import ManagedScenes
  journal=self.addon.command_journal
  journal.claim({'id':'historic','entity_id':'light.old','action':'turn_off','params':{}})
  journal.complete('historic','service_accepted')
  row=journal.db.execute('SELECT * FROM commands WHERE id=?',('historic',)).fetchone()
  schema=journal.db.execute('PRAGMA table_info(commands)').fetchall()
  scenes_path=Path(self.tmp.name)/'scenes.sqlite3'
  scenes=ManagedScenes(scenes_path)
  scenes.save('scene.lapahub_old',{'scene_id':'lapahub_old','entities':{'light.old':{'state':'off'}}})
  scenes.suppress('scene.lapahub_old');scenes.close()
  before=(scenes_path.stat().st_ino,hashlib.sha256(scenes_path.read_bytes()).hexdigest())
  self.lookup_status=404
  await self.addon.execute_command({'id':'missing-new','entity_id':'switch.missing','action':'turn_on','params':{}})
  self.assertEqual(journal.db.execute('SELECT * FROM commands WHERE id=?',('historic',)).fetchone(),row)
  self.assertEqual(journal.db.execute('PRAGMA table_info(commands)').fetchall(),schema)
  self.assertEqual((scenes_path.stat().st_ino,hashlib.sha256(scenes_path.read_bytes()).hexdigest()),before)
  with sqlite3.connect(scenes_path) as db:
   self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
   self.assertEqual(db.execute('SELECT active FROM scenes').fetchall(),[(0,)])
  self.assertEqual(self.requests,[])

 async def test_scene_restore_post_then_missing_activation_preserves_uncertain_dispatch(self):
  from managed_scenes import ManagedScenes
  self.addon.managed_scenes=ManagedScenes(Path(self.tmp.name)/'multi-scenes.sqlite3')
  self.addon.scene_lock=asyncio.Lock()
  definition={'scene_id':'lapahub_fixture','entities':{'light.fixture':{'state':'on'}}}
  self.addon.managed_scenes.save('scene.lapahub_fixture',definition)
  self.addon.get_ha_states=AsyncMock(return_value=[{'entity_id':'light.fixture','state':'off'}])
  self.lookup_status=404
  try:
   await self.addon.execute_command({'id':'scene-multi','entity_id':'scene.lapahub_fixture','action':'scene.turn_on','params':{}})
   self.assertEqual(self.requests,[('scene','create',definition)])
   self.assertEqual(self.lookup_calls,['scene.lapahub_fixture'])
   self.assertIn(('scene-multi','unknown'),self.addon.command_journal.pending())
   self.assertIn('addonDispatchedAt',self.addon.command_journal.trace('scene-multi'))
  finally:self.addon.managed_scenes.close()

 async def test_multi_post_second_preflight_failure_never_erases_first_dispatch(self):
  first_trace=[]
  async def multi(command):
   self.assertTrue(await self.addon.call_ha_service('switch','turn_on',{'entity_id':'switch.first'}))
   first_trace.append(self.addon.command_journal.trace(command['id'])['addonDispatchedAt'])
   self.lookup_status=404
   await self.addon.call_ha_service('switch','turn_off',{'entity_id':'switch.second'})
  self.addon._execute_command_once=multi
  await self.addon.execute_command({'id':'multi-post','entity_id':'switch.first','action':'turn_on','params':{}})
  self.assertEqual(self.requests,[('switch','turn_on',{'entity_id':'switch.first'})])
  self.assertIn(('multi-post','unknown'),self.addon.command_journal.pending())
  self.assertEqual(self.addon.command_journal.trace('multi-post')['addonDispatchedAt'],first_trace[0])

 async def test_post_wait_respects_original_remaining_budget_and_retains_uncertainty(self):
  import time
  self.post_gate=asyncio.Event()
  started=time.monotonic()
  await asyncio.wait_for(self.addon.execute_command({'id':'post-deadline','entity_id':'switch.fixture','action':'turn_on','params':{},'expires_at':time.time()+.15}),.6)
  self.assertLess(time.monotonic()-started,.5)
  self.assertTrue(self.post_entered.is_set())
  self.assertEqual(len(self.requests),1)
  self.assertIn(('post-deadline','unknown'),self.addon.command_journal.pending())
  self.assertIn('addonDispatchedAt',self.addon.command_journal.trace('post-deadline'))
  self.post_gate.set()
