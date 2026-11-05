"""Recover only jobs proved stopped; interrupted publication keeps ownership proof."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
import onepve_agent as agent
import onepve_common as common


def result(text='',code=0):
    return {'returncode':code,'stdout':text,'stderr':''}


class JobRecoveryTests(unittest.TestCase):
    def state(self,status=None):
        owner='a'*32;unit='onepve-driver-'+owner[:12]+'-1'
        job={'name':'driver','unit':unit,'script':'/stage/'+unit+'.py',
             'path':'/jobs/'+unit+'.json','command':['/usr/bin/python3','/stage/prepare-driver.py']}
        if status:job['result']={'status':status}
        return {'vmid':103,'owner':owner,'operation':'create','phase':'GUEST_DRIVER_RUNNING',
                'guest_stage':'/stage','config':{},'jobs':{'driver':job},'job_history':[dict(job)]}

    def guest(self,state,stopped=True):
        q=mock.MagicMock();q.__enter__.return_value=q
        rows={state['jobs']['driver']['path']:{'status':'RUNNING','boot_id':'old-boot'}}
        launches=[]
        def execute(argv,timeout):
            if argv[0]=='/bin/systemctl':
                return result('LoadState=not-found\n' if stopped or argv[2].endswith('-2.service')
                              else 'LoadState=loaded\nActiveState=activating\n')
            if argv[0]=='/usr/bin/test':return result(code=0 if argv[-1] in rows else 1)
            if argv[0]=='/usr/bin/python3':
                compile(argv[2],'<job-observer>','exec')
                return result(json.dumps({'stopped':stopped,'boot_id':'new-boot','processes':[] if stopped else [123]}))
            if argv[0]=='/usr/bin/systemd-run':
                launches.append(argv)
                path=state['jobs']['driver']['path'];rows[path]={'status':'SUCCESS','returncode':0}
                return result()
            raise AssertionError(argv)
        q.execute.side_effect=execute
        def read(path):
            if path not in rows:raise RuntimeError('not found')
            return json.dumps(rows[path]).encode()
        q.read.side_effect=read
        return q,rows,launches

    def test_missing_unit_and_running_record_is_interrupted_without_restart(self):
        s=self.state();q,rows,launches=self.guest(s)
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'):
            with self.assertRaisesRegex(TimeoutError,'interrupted; explicit resume'):
                agent._job(s,'driver',s['jobs']['driver']['command'])
        self.assertEqual(s['jobs']['driver']['result']['status'],'INTERRUPTED')
        self.assertEqual(s['jobs']['driver']['result']['previous_boot_id'],'old-boot')
        self.assertEqual(s['jobs']['driver']['result']['observed_boot_id'],'new-boot')
        self.assertEqual(s['job_history'][0]['result']['status'],'INTERRUPTED')
        self.assertEqual(rows[s['jobs']['driver']['path']]['status'],'RUNNING')
        self.assertEqual(s['phase'],'GUEST_DRIVER_RUNNING')
        self.assertEqual(launches,[]);q.write.assert_not_called()

    def test_explicit_resume_rechecks_stopped_job_then_creates_new_attempt(self):
        s=self.state();old=dict(s['jobs']['driver']);q,rows,launches=self.guest(s)
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'):
            answer=agent._job(s,'driver',old['command'],retry_failed=True)
        self.assertEqual(answer['status'],'SUCCESS')
        self.assertTrue(s['jobs']['driver']['unit'].endswith('-2'))
        self.assertEqual(s['job_history'][0]['result']['status'],'INTERRUPTED')
        self.assertEqual(s['job_history'][0]['path'],old['path'])
        self.assertEqual(rows[old['path']]['status'],'RUNNING')
        self.assertEqual(len(launches),1);self.assertEqual(q.write.call_count,1)
        wrapper=q.write.call_args.args[1].decode();compile(wrapper,'<wrapper>','exec')
        self.assertIn("'boot_id'",wrapper);self.assertIn("'started_utc'",wrapper);self.assertIn("'finished_utc'",wrapper)

    def test_explicit_resume_recovers_an_intent_without_a_result_file(self):
        s=self.state();old=dict(s['jobs']['driver']);q,rows,launches=self.guest(s);rows.clear()
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'):
            answer=agent._job(s,'driver',old['command'],retry_failed=True)
        self.assertEqual(answer['status'],'SUCCESS');self.assertEqual(len(launches),1)
        self.assertEqual(s['job_history'][0]['result']['status'],'INTERRUPTED')
        self.assertTrue(s['jobs']['driver']['unit'].endswith('-2'))

    def test_missing_result_does_not_restart_an_active_old_wrapper(self):
        s=self.state();q,rows,launches=self.guest(s,stopped=False);rows.clear()
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'):
            with self.assertRaisesRegex(TimeoutError,'outcome unavailable'):
                agent._job(s,'driver',s['jobs']['driver']['command'],retry_failed=True)
        self.assertNotIn('result',s['jobs']['driver']);self.assertEqual(launches,[]);q.write.assert_not_called()

    def test_live_original_is_observed_without_restarting_or_replacing_inputs(self):
        s=self.state();q,rows,launches=self.guest(s,stopped=False);now=[0.0]
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'), \
             mock.patch.object(agent.time,'monotonic',side_effect=lambda:now[0]), \
             mock.patch.object(agent.time,'sleep',side_effect=lambda seconds:now.__setitem__(0,20)):
            with self.assertRaisesRegex(TimeoutError,'still running/uncertain'):
                agent._job(s,'driver',s['jobs']['driver']['command'],timeout=10,retry_failed=True)
        self.assertEqual(len(s['job_history']),1);self.assertEqual(launches,[]);q.write.assert_not_called()
        self.assertNotIn('result',s['jobs']['driver'])

    def test_lost_observation_does_not_label_a_job_failed_or_restart_it(self):
        s=self.state();q,rows,launches=self.guest(s)
        q.execute.side_effect=ConnectionResetError('guest offline')
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'):
            with self.assertRaisesRegex(TimeoutError,'outcome unavailable'):
                agent._job(s,'driver',s['jobs']['driver']['command'],retry_failed=True)
        self.assertNotIn('result',s['jobs']['driver']);self.assertEqual(launches,[]);q.write.assert_not_called()

    def test_stopped_observation_rechecks_a_racing_terminal_result(self):
        s=self.state();q,rows,launches=self.guest(s)
        q.read.side_effect=[b'{"status":"RUNNING"}',b'{"status":"SUCCESS","returncode":0}']
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'):
            answer=agent._job(s,'driver',s['jobs']['driver']['command'],retry_failed=True)
        self.assertEqual(answer['status'],'SUCCESS');self.assertEqual(launches,[]);q.write.assert_not_called()

    def test_uncertain_driver_errors_do_not_fallback_to_finished(self):
        for error in [TimeoutError('still running'),ConnectionResetError('guest offline'),common.GuestAgentError('query unavailable'),subprocess.TimeoutExpired('observer',30)]:
            s=self.state()
            with self.subTest(error=type(error).__name__),mock.patch.object(agent,'_owned'), \
                 mock.patch.object(agent,'_prepare_driver',side_effect=error),mock.patch.object(agent,'_fallback') as fallback:
                with self.assertRaises(type(error)):agent._continue(s)
            fallback.assert_not_called();self.assertEqual(s['phase'],'GUEST_DRIVER_RUNNING')

    def test_known_driver_failure_still_uses_software_fallback(self):
        s=self.state()
        with mock.patch.object(agent,'_owned'),mock.patch.object(agent,'_prepare_driver',side_effect=RuntimeError('confirmed failure')), \
             mock.patch.object(agent,'_fallback') as fallback:
            agent._continue(s)
        fallback.assert_called_once()

    def test_guest_protocol_errors_have_a_distinct_runtime_subclass(self):
        q=object.__new__(common.Qga);q.sequence=0;q.stream=io.BytesIO()
        q._read=mock.Mock(return_value={'id':1,'error':{'class':'GenericError','desc':'guest query unavailable'}})
        with self.assertRaises(common.GuestAgentError) as raised:q.call('guest-exec-status',{'pid':123})
        self.assertIsInstance(raised.exception,RuntimeError)


class BundleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(agent,'AGENT_STATE',self.root/'state'))
        self.stack.enter_context(mock.patch.object(agent,'_validate_artifacts'))
        self.repo=self.root/'repo';(self.repo/'guest').mkdir(parents=True);(self.repo/'guest/install.sh').write_text('fixture')
        (self.repo/'lib').mkdir()
        for name in ['onepve_common.py','onepve_gpu.py','onepve_driver_cache.py']:(self.repo/'lib'/name).write_text('fixture')
        self.stack.enter_context(mock.patch.object(agent,'repo_root',return_value=self.repo))
        cache=self.root/'runtime';cache.mkdir();(cache/'manifest.json').write_text('{}')
        source=self.root/'driver.tar.gz';source.write_bytes(b'driver')
        self.state={'vmid':103,'owner':'a'*32,'phase':'PLANNED',
                    'artifacts':{'runtime_cache':str(cache),'driver_sources':{'pinned':str(source)}}}
        self.directory=agent._state_path(103).parent;self.directory.mkdir(mode=0o700,parents=True);self.directory.chmod(0o700)
        self.path=self.directory/('payload-'+self.state['owner']+'.tar.gz')
        self.save=agent._save
        if os.name=='posix' and os.geteuid()!=0:self.skipTest('root-owned bundle directory checks require root')

    def test_interrupted_build_cleans_its_partial_and_can_retry(self):
        with mock.patch.object(tarfile.TarFile,'add',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):agent._bundle(self.state)
        self.assertFalse(self.path.exists());self.assertEqual(list(self.directory.glob('.payload-*.part-*')),[])
        self.assertNotIn('bundle_sha256',self.state)
        self.assertEqual(agent._bundle(self.state),self.path)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),self.state['bundle_sha256'])

    def test_publication_survives_exit_before_final_state_save(self):
        def save(state,phase=None,**changes):
            if 'bundle_sha256' in changes:raise OSError('state write interrupted')
            return self.save(state,phase,**changes)
        with mock.patch.object(agent,'_save',side_effect=save):
            with self.assertRaisesRegex(OSError,'state write interrupted'):agent._bundle(self.state)
        self.assertTrue(self.path.is_file());self.assertEqual(list(self.directory.glob('.payload-*.part-*')),[])
        restarted=json.loads(agent._state_path(103).read_text())
        self.assertNotIn('bundle_sha256',restarted);self.assertIn('bundle_pending',restarted)
        before=self.path.read_bytes()
        with mock.patch.object(tarfile,'open',side_effect=AssertionError('do not repack a proven publication')):
            self.assertEqual(agent._bundle(restarted),self.path)
        self.assertEqual(self.path.read_bytes(),before);self.assertIsNone(restarted['bundle_pending'])
        self.assertEqual(restarted['bundle_sha256'],hashlib.sha256(before).hexdigest())

    def test_unrecorded_or_changed_publication_is_never_overwritten(self):
        self.path.write_bytes(b'foreign');self.path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError,'publication proof'):agent._bundle(self.state)
        self.assertEqual(self.path.read_bytes(),b'foreign')
        self.state['bundle_pending']={'owner':self.state['owner'],'path':str(self.path),'sha256':'0'*64,'bytes':7}
        with self.assertRaisesRegex(RuntimeError,'publication proof'):agent._bundle(self.state)
        self.assertEqual(self.path.read_bytes(),b'foreign')

    def test_publish_race_retains_foreign_destination_and_cleans_own_partial(self):
        def race(source,destination):
            destination.write_bytes(b'foreign');raise FileExistsError('appeared')
        with mock.patch.object(agent.os,'link',side_effect=race):
            with self.assertRaisesRegex(RuntimeError,'different content'):agent._bundle(self.state)
        self.assertEqual(self.path.read_bytes(),b'foreign');self.assertEqual(list(self.directory.glob('.payload-*.part-*')),[])


if __name__=='__main__':unittest.main()
