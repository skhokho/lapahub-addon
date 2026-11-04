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
from unittest.mock import AsyncMock, Mock
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
 names={'execute_command','_execute_command_once','call_ha_service','report_command_result','handle_state_change','sync_devices','push_state_change_to_cloud','push_devices_to_cloud'}
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
  self.requests=[];self.acks=[];self.status=200;self.cloud_states=[];self.cloud_status=200;self.ack_status=200
  async def service(request):
   self.assertEqual(request.headers['Authorization'],'Bearer synthetic-ha')
   payload=await request.json();self.requests.append((request.match_info['domain'],request.match_info['service'],payload))
   return web.json_response([],status=self.status)
  async def acknowledgement(request):
   self.assertEqual(request.headers['Authorization'],'Bearer synthetic-cloud')
   self.acks.append(await request.json());return web.json_response({},status=self.ack_status)
  async def cloud_state(request):
   self.assertEqual(request.headers['Authorization'],'Bearer synthetic-cloud')
   self.cloud_states.append(await request.json());return web.json_response({},status=self.cloud_status)
  app=web.Application();app.router.add_post('/cloud/updateDeviceState',cloud_state);app.router.add_post('/api/services/{domain}/{service}',service);app.router.add_post('/cloud/reportCommandResult',acknowledgement)
  self.runner=web.AppRunner(app);await self.runner.setup();site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
  self.base='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1]);self.session=aiohttp.ClientSession(trust_env=False)
  class Gateway:
   def post(inner,url,**kwargs):
    if url.startswith('https://us-central1-'):url=self.base+'/cloud/'+url.rsplit('/',1)[-1]
    return self.session.post(url,**kwargs)
  self.tmp=tempfile.TemporaryDirectory();self.addon=actual_type(self.base)()
  self.addon.session=Gateway();self.addon.command_journal=CommandJournal(Path(self.tmp.name)/'journal.sqlite3')
  self.addon.firebase_credentials={'token':'synthetic-cloud'};self.addon.firebase_project='synthetic';self.addon.hub_id='synthetic'
  self.addon.devices={};self.addon.sensor_realtime=False;self.addon.state_publisher=SimpleNamespace(offer=Mock(),dropped=0)
  self.addon.entity_device_map={};self.addon.physical_devices={};self.addon.push_devices_to_cloud=AsyncMock()

 async def asyncTearDown(self):
  await self.session.close();await self.runner.cleanup();self.addon.command_journal.close();self.tmp.cleanup()

 async def test_every_admitted_non_camera_service_exact_http_and_ack(self):
  index=0
  for domain,services in CASES.items():
   for action,params in services.items():
    with self.subTest(domain=domain,action=action):
     index+=1;command_id='matrix-'+str(index);entity=domain+'.fixture'
     await self.addon.execute_command({'id':command_id,'entity_id':entity,'action':action,'params':params})
     expected_service={'open':'open_cover','close':'close_cover'}.get(action,action)
     self.assertEqual(self.requests[-1],(domain,expected_service,{**params,'entity_id':entity}))
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
