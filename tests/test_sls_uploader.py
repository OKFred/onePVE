"""SLS orchestration with real SQLite/budgets and an in-memory HTTPS peer."""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
import onepve_audit as audit
import onepve_sls as sls
import onepve_sls_budget as budget
import onepve_sls_wire as wire
from onepve_common import atomic_json

ACCOUNT='1234567890123456'
SECRET='never-print-this-secret'
AK='test-access-key-id'
ENDPOINT='cn-hangzhou.log.aliyuncs.com'
PROJECT='onepve-test'
STORE='connections'
BOOT='450445aa-3f2d-4130-999a-a75ec58b694f'


@unittest.skipIf(os.name=='posix' and os.geteuid()!=0,'PVE private resources require root')
class SlsUploaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.units=self.root/'units';self.units.mkdir()
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(sls,'ROOT',self.root/'sls'))
        self.stack.enter_context(mock.patch.object(sls,'AGENT_STATE',self.root/'agent'))
        self.stack.enter_context(mock.patch.object(sls,'UNIT_ROOT',self.units))
        self.stack.enter_context(mock.patch.object(sls,'require_root'))
        self.command=self.stack.enter_context(mock.patch.object(sls,'run',return_value=types.SimpleNamespace(returncode=0)))
        self.stack.enter_context(mock.patch.object(audit,'AUDIT_ROOT',self.root/'audit'))
        self.stack.enter_context(mock.patch.object(audit,'_load',return_value={'enabled':True}))
        if budget.fcntl is None:
            self.stack.enter_context(mock.patch.object(budget,'fcntl',types.SimpleNamespace(flock=lambda *args:None,LOCK_EX=2)))
        self.credentials=self.root/'credentials.json'
        atomic_json(self.credentials,dict(access_key_id=AK,access_key_secret=SECRET))
        self.requests=[];self.raw_posts=[];self.network_error=None;self.metering='ChargeByFunction'
        self.project_owner=ACCOUNT
        self.stack.enter_context(mock.patch.object(wire.http.client,'HTTPSConnection',side_effect=self.connection))

    def connection(self,*args,**unused):
        test=self
        class Response:
            def __init__(self,status,payload):self.status=status;self.payload=payload
            def read(self,limit):return self.payload[:limit]
            def getheader(self,name):return 'test-request-id'
        class Connection:
            def request(self,method,path,body=None,headers=None):
                # A real durable reservation must precede every fake network operation.
                usage=budget.status(sls.ROOT/'budget',ACCOUNT)
                test.assertGreaterEqual(usage['requests_used'],len(test.requests)+1)
                self.method,self.path=method,path;test.requests.append((method,path))
                if method=='POST':
                    raw=zlib.decompress(body);test.raw_posts.append(raw)
                    if test.network_error:raise test.network_error
            def getresponse(self):
                if self.method=='POST':return Response(200,b'')
                if self.path=='/':payload=dict(projectName=PROJECT,region='cn-hangzhou',status='Normal',owner=test.project_owner)
                elif self.path.endswith('/meteringmode'):payload=dict(meteringMode=test.metering)
                elif self.path.endswith('/shards'):payload=[dict(status='readwrite',shardID=0)]
                elif self.path.endswith('/index'):return Response(404,b'{"errorCode":"IndexConfigNotExist"}')
                else:payload=dict(logstoreName=STORE,mode='standard',ttl=3,shardCount=1,autoSplit=False,enable_tracking=False,processorId='')
                return Response(200,json.dumps(payload).encode())
            def close(self):pass
        return Connection()

    def configure(self):return sls.configure(ACCOUNT,ENDPOINT,PROJECT,STORE,self.credentials)

    def start(self,requests=100000):
        self.configure();sls.approve_budget(verified=True,requests=requests);return sls.enable()

    def source(self,vmid=103,owner=None):
        value=dict(vmid=vmid,owner=owner or ('a' if vmid==103 else 'b')*32)
        atomic_json(sls.AGENT_STATE/str(vmid)/'state.json',value)
        directory=audit._paths(vmid)[0];directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        return value

    def observe(self,vmid=103,port=52525,event='NEW',observed=None):
        ip='192.168.50.'+str(vmid)
        line=(f'[{event}] ipv4 2 tcp 6 120 ESTABLISHED src={ip} dst=1.1.1.1 sport={port} dport=443 '
              f'packets=1 bytes=60 src=1.1.1.1 dst={ip} sport=443 dport={port} packets=1 bytes=60 id={port}')
        flow=audit.parse_flow(line,ip,BOOT,observed or audit.utc())
        with contextlib.closing(audit._database(audit._paths(vmid)[2],True)) as db:
            audit._observe(db,flow);db.commit()

    def upload_state(self):return json.loads((sls.ROOT/'upload-state.json').read_text())

    def test_disabled_default_is_no_network_no_source_scan_and_no_resources(self):
        before=set(self.root.iterdir())
        with mock.patch.object(sls,'_rows',side_effect=AssertionError('Disabled source scan')):
            self.assertFalse(sls.status()['enabled']);self.assertFalse(sls.upload()['enabled'])
        self.assertEqual(set(self.root.iterdir()),before);self.assertEqual(self.requests,[])
        self.command.assert_not_called()

    def test_local_configuration_redacts_credentials_and_requires_explicit_budget(self):
        answer=self.configure()
        self.assertFalse(answer['enabled']);self.assertEqual(self.requests,[])
        for value in [answer,json.loads((sls.ROOT/'config.json').read_text())]:
            rendered=json.dumps(value);self.assertNotIn(SECRET,rendered);self.assertNotIn(AK,rendered)
        with self.assertRaises(RuntimeError):sls.approve_budget(verified=False)
        with self.assertRaises(RuntimeError):sls.enable()
        self.assertEqual(self.requests,[]);self.assertEqual(list(self.units.iterdir()),[])

    def test_enable_uses_metadata_only_then_retains_budget_and_runtime_on_disable(self):
        self.start();before=budget.status(sls.ROOT/'budget',ACCOUNT)
        self.assertEqual(before['requests_used'],5);self.assertEqual(before['raw_bytes_used'],0)
        self.assertTrue(all(method=='GET' for method,path in self.requests))
        cfg=json.loads((sls.ROOT/'config.json').read_text())
        runtime=Path(cfg['runtime']);self.assertTrue(runtime.is_dir())
        self.assertEqual({path.name for path in runtime.iterdir()},set(sls.SOURCES)|{'manifest.json'})
        service=(self.units/'onepve-sls-upload.service').read_text()
        self.assertIn(str(runtime/'onepve_sls.py'),service)
        self.assertIn('PYTHONDONTWRITEBYTECODE=1',service)
        self.assertIn('OnUnitActiveSec=5min',(self.units/'onepve-sls-upload.timer').read_text())
        sls.disable();self.assertFalse(sls.status()['enabled'])
        self.assertEqual(budget.status(sls.ROOT/'budget',ACCOUNT),before)
        self.assertTrue(runtime.is_dir());self.assertEqual(len(self.requests),5)

    def test_enabled_status_checks_units_and_timer_without_changing_resources(self):
        self.start();service=self.units/'onepve-sls-upload.service';saved=service.read_bytes()
        usage=budget.status(sls.ROOT/'budget',ACCOUNT)
        for scenario in ['healthy','timer_stopped','unit_drift']:
            service.write_bytes(saved if scenario!='unit_drift' else saved+b'# external change\n')
            before={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in sls.ROOT.rglob('*') if path.is_file()}
            self.command.reset_mock()
            self.command.return_value=types.SimpleNamespace(returncode=3 if scenario=='timer_stopped' else 0)
            answer=sls.status()
            self.assertEqual(answer['status'],'ENABLED' if scenario=='healthy' else 'ATTENTION')
            self.assertEqual(answer['units_match'],scenario!='unit_drift')
            self.assertEqual(answer['timer_active'],scenario=='healthy')
            for call in self.command.call_args_list:
                self.assertEqual(call.args[0],['systemctl','is-active','--quiet','onepve-sls-upload.timer'])
                self.assertFalse(call.kwargs['check'])
            after={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in sls.ROOT.rglob('*') if path.is_file()}
            self.assertEqual(after,before);self.assertEqual(budget.status(sls.ROOT/'budget',ACCOUNT),usage)
            self.assertEqual(len(self.requests),5)

    def test_systemd_enable_failure_leaves_upload_disabled_and_explicitly_recoverable(self):
        self.configure();sls.approve_budget(verified=True)
        def execute(argv,**unused):
            if argv[:3]==['systemctl','enable','--now']:raise RuntimeError('service enable failed')
            return types.SimpleNamespace(returncode=0)
        with mock.patch.object(sls,'run',side_effect=execute):
            with self.assertRaisesRegex(RuntimeError,'enable failed'):sls.enable()
        self.assertFalse(json.loads((sls.ROOT/'config.json').read_text())['enabled'])
        self.assertEqual(self.upload_state()['halt']['reason'],'EnableServiceFailed')
        usage=budget.status(sls.ROOT/'budget',ACCOUNT);self.assertEqual(usage['requests_used'],5)
        self.assertFalse(sls.upload()['enabled']);self.assertEqual(len(self.requests),5)
        self.assertEqual(budget.status(sls.ROOT/'budget',ACCOUNT),usage)
        self.assertTrue(sls.enable()['enabled'])
        self.assertIsNone(self.upload_state()['halt'])
        self.assertEqual(budget.status(sls.ROOT/'budget',ACCOUNT)['requests_used'],10)

    def test_unowned_same_name_units_are_never_stopped_or_disabled(self):
        self.configure()
        originals={}
        for name in ['onepve-sls-upload.service','onepve-sls-upload.timer']:
            path=self.units/name;path.write_text('# owned by another tool\n[Unit]\nDescription=foreign\n')
            originals[path]=path.read_bytes()
        for action in [sls.enable,sls.disable]:
            self.command.reset_mock()
            with self.subTest(action=action.__name__),self.assertRaises(RuntimeError):action()
            self.command.assert_not_called();self.assertEqual(self.requests,[])
            for path,data in originals.items():self.assertEqual(path.read_bytes(),data)

    def test_preflight_failure_or_wrong_account_never_puts_or_installs_units(self):
        for failure in ['metering','owner']:
            with self.subTest(failure=failure):
                if not (sls.ROOT/'config.json').exists():
                    self.configure();sls.approve_budget(verified=True)
                self.metering='ChargeByDataIngest' if failure=='metering' else 'ChargeByFunction'
                self.project_owner='9876543210987654' if failure=='owner' else ACCOUNT
                with self.assertRaises(RuntimeError):sls.enable()
                self.assertEqual(self.raw_posts,[]);self.assertFalse(sls.status()['enabled'])
                self.assertEqual(list(self.units.iterdir()),[])

    def test_metadata_and_put_share_a_durable_cross_vm_budget(self):
        self.start();self.source(103);self.source(104)
        self.observe(103);self.observe(104)
        before={vmid:audit._paths(vmid)[2].read_bytes() for vmid in [103,104]}
        result=sls.upload();self.assertEqual(result['rows'],2)
        usage=budget.status(sls.ROOT/'budget',ACCOUNT)
        self.assertEqual(usage['requests_used'],12)
        self.assertEqual(usage['raw_bytes_used'],sum(map(len,self.raw_posts)))
        self.assertEqual(len(self.raw_posts),2)
        state=self.upload_state();self.assertEqual(set(state['cursors']),{'103:'+'a'*32,'104:'+'b'*32})
        for vmid in [103,104]:self.assertEqual(audit._paths(vmid)[2].read_bytes(),before[vmid])
        self.assertEqual(sls.upload()['rows'],0);self.assertEqual(usage,budget.status(sls.ROOT/'budget',ACCOUNT))

    def test_many_normal_flows_publish_a_bounded_prefix_instead_of_halting(self):
        self.start();self.source()
        for port in range(52000,52100):self.observe(port=port)
        first=sls.upload()
        self.assertGreater(first['rows'],0);self.assertLess(first['rows'],100)
        self.assertTrue(all(len(raw)<=wire.MAX_RAW_BYTES for raw in self.raw_posts))
        second=sls.upload();self.assertEqual(first['rows']+second['rows'],100)
        self.assertIsNone(self.upload_state()['halt'])

    def test_status_is_read_only_and_does_not_probe_cloud_or_create_resources(self):
        self.configure();before={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in sls.ROOT.rglob('*') if path.is_file()}
        self.command.reset_mock()
        for unused in range(2):self.assertFalse(sls.status()['enabled'])
        after={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in sls.ROOT.rglob('*') if path.is_file()}
        self.assertEqual(after,before);self.assertEqual(self.requests,[]);self.command.assert_not_called()

    def test_in_place_end_updates_and_reused_ids_are_selected_by_observation_watermark(self):
        self.start();source=self.source();now=datetime.now(timezone.utc)
        self.observe(observed=audit.utc(now));sls.upload()
        previous=self.upload_state()['cursors']['103:'+'a'*32]
        self.observe(event='DESTROY',observed=audit.utc(now+timedelta(seconds=1)))
        rows=sls._rows(source,previous)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['id'],previous[1]);self.assertEqual(rows[0]['event'],'DESTROY')
        sls.upload();self.assertEqual(len(self.raw_posts),2)
        with contextlib.closing(audit._database(audit._paths(103)[2],True)) as db:
            db.execute('DELETE FROM flows');db.commit()
        self.observe(port=52526,observed=audit.utc(now+timedelta(seconds=2)))
        rows=sls._rows(source,self.upload_state()['cursors']['103:'+'a'*32])
        self.assertEqual(rows[0]['id'],1);self.assertGreater(rows[0]['last_seen_utc'],previous[0])
        sls.upload();self.assertEqual(len(self.raw_posts),3)

    def test_equal_timestamp_rows_are_not_skipped_by_cursor_tie_breaker(self):
        self.configure();source=self.source();observed=audit.utc()
        for port in [52525,52526]:self.observe(port=port,observed=observed)
        first=sls._rows(source,['',0],limit=1)[0]
        following=sls._rows(source,[first['last_seen_utc'],first['id']],limit=1)
        self.assertEqual(len(following),1);self.assertGreater(following[0]['id'],first['id'])
        cfg=json.loads((sls.ROOT/'config.json').read_text())
        one=sls._event(cfg,source,first);again=sls._event(cfg,source,first)
        self.assertEqual(one['contents']['event_id'],again['contents']['event_id'])
        other=sls._event(cfg,dict(source,owner='b'*32),first)
        self.assertNotEqual(one['contents']['event_id'],other['contents']['event_id'])

    def test_exhausted_budget_halts_without_another_network_attempt(self):
        self.start(requests=5);self.source();self.observe();before=len(self.requests)
        with self.assertRaisesRegex(RuntimeError,'halted'):sls.upload()
        self.assertEqual(len(self.requests),before);self.assertEqual(self.raw_posts,[])
        self.assertEqual(sls.upload()['status'],'HALTED');self.assertEqual(len(self.requests),before)
        self.assertEqual(self.upload_state()['cursors'],{})

    def test_unknown_post_outcome_never_advances_cursor_or_auto_retries(self):
        self.start();self.source();self.observe();self.network_error=TimeoutError(SECRET)
        with self.assertRaises(RuntimeError) as error:sls.upload()
        self.assertNotIn(SECRET,str(error.exception));self.assertEqual(self.upload_state()['cursors'],{})
        usage=budget.status(sls.ROOT/'budget',ACCOUNT);before=len(self.requests)
        self.assertGreater(usage['raw_bytes_used'],0);self.assertEqual(sls.upload()['status'],'HALTED')
        self.assertEqual(len(self.requests),before);self.assertEqual(budget.status(sls.ROOT/'budget',ACCOUNT),usage)

    def test_process_exit_between_reserve_and_confirmation_requires_explicit_reenable(self):
        self.start();self.source();self.observe();self.network_error=SystemExit('simulated process termination')
        with self.assertRaises(SystemExit):sls.upload()
        state=self.upload_state();self.assertIsNotNone(state['pending']);self.assertEqual(state['cursors'],{})
        usage=budget.status(sls.ROOT/'budget',ACCOUNT);requests=len(self.requests)
        report=sls.upload();self.assertEqual(report['status'],'HALTED')
        self.assertEqual(report['halt']['reason'],'PreviousUploadUncertain')
        self.assertEqual(len(self.requests),requests);self.assertEqual(budget.status(sls.ROOT/'budget',ACCOUNT),usage)
        self.network_error=None;sls.enable()
        self.assertIsNone(self.upload_state()['pending']);self.assertIsNone(self.upload_state()['halt'])
        self.assertEqual(budget.status(sls.ROOT/'budget',ACCOUNT)['raw_bytes_used'],usage['raw_bytes_used'])
        sls.upload();self.assertEqual(len(self.raw_posts),2);self.assertEqual(self.raw_posts[0],self.raw_posts[1])

    @unittest.skipUnless(os.name=='posix','POSIX credential access boundary')
    def test_publicly_readable_credentials_are_refused_before_any_cloud_attempt(self):
        self.configure();sls.approve_budget(verified=True);self.credentials.chmod(0o644)
        before=budget.status(sls.ROOT/'budget',ACCOUNT)
        with self.assertRaises(RuntimeError):sls.enable()
        self.assertEqual(self.requests,[]);self.assertEqual(budget.status(sls.ROOT/'budget',ACCOUNT),before)
        self.assertFalse(sls.status()['enabled'])

    def test_save_failure_after_confirmed_put_persists_halt_and_does_not_retry(self):
        self.start();self.source();self.observe();original=sls.atomic_json;interrupted=False
        def write(path,value,*args,**kwargs):
            nonlocal interrupted
            if Path(path).name=='upload-state.json' and value.get('cursors') and not value.get('halt') and not interrupted:
                interrupted=True;raise OSError('state write interrupted')
            return original(path,value,*args,**kwargs)
        with mock.patch.object(sls,'atomic_json',side_effect=write):
            with self.assertRaisesRegex(RuntimeError,'halted'):sls.upload()
        self.assertEqual(len(self.raw_posts),1);self.assertIsNotNone(self.upload_state()['halt'])
        self.assertEqual(sls.upload()['status'],'HALTED');self.assertEqual(len(self.raw_posts),1)

    def test_missing_or_corrupt_cursor_cannot_silently_replay_history(self):
        self.start();self.source();self.observe();path=sls.ROOT/'upload-state.json';saved=path.read_bytes()
        for value in [None,b'{broken']:
            if value is None:path.unlink(missing_ok=True)
            else:path.write_bytes(value)
            with self.subTest(value=value),self.assertRaises((RuntimeError,OSError,ValueError)):sls.upload()
            self.assertEqual(self.raw_posts,[])
        path.write_bytes(saved)

    def test_configuration_retry_recovers_initial_cursor_publication_failure(self):
        original=sls.atomic_json;interrupted=False
        def write(path,value,*args,**kwargs):
            nonlocal interrupted
            if Path(path).name=='upload-state.json' and not interrupted:
                interrupted=True;raise OSError('publication interrupted')
            return original(path,value,*args,**kwargs)
        with mock.patch.object(sls,'atomic_json',side_effect=write):
            with self.assertRaises(OSError):self.configure()
        # This is an explicit retry, not an implicit cursor reset during upload.
        self.assertFalse(self.configure()['enabled']);self.assertEqual(self.requests,[])

    def test_credential_rotation_and_reconfigure_preserve_consumed_budget(self):
        self.start();usage=budget.status(sls.ROOT/'budget',ACCOUNT)
        atomic_json(self.credentials,dict(access_key_id=AK,access_key_secret=SECRET+'-rotated'))
        self.configure();self.assertEqual(budget.status(sls.ROOT/'budget',ACCOUNT),usage)
        self.assertEqual(self.requests,[('GET','/'),('GET','/logstores/'+STORE),('GET','/logstores/'+STORE+'/meteringmode'),
                                        ('GET','/logstores/'+STORE+'/shards'),('GET','/logstores/'+STORE+'/index')])
        with self.assertRaises(RuntimeError):sls.approve_budget(verified=True)


if __name__=='__main__':unittest.main()
