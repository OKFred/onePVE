"""Diagnostics observe existing state; they never run browser acceptance or repair."""
import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
import onepve_agent as agent


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(agent,'AGENT_STATE',Path(self.temp.name)))
        self.stack.enter_context(mock.patch.object(agent,'require_root'))
        self.state={'vmid':103,'owner':'a'*32,'phase':'READY_GPU','mode':'wayland',
                    'verification':{'gpu':{'runtime':{'status':'PASS_GPU_RUNTIME'},'mcp':{'status':'passed'}}},
                    'jobs':{'install':{'path':'/var/lib/onepve-agent/jobs/install-1.json'}}}
        self.path=agent._state_path(103);self.path.parent.mkdir();self.path.write_text(json.dumps(self.state))
        self.before=self.path.read_bytes();self.before_mtime=self.path.stat().st_mtime_ns
        self.owned=self.stack.enter_context(mock.patch.object(agent,'_owned',return_value={}))
        for name in ['_save','_runtime_verify','_create_vm','_job','_stop']:
            self.stack.enter_context(mock.patch.object(agent,name,side_effect=AssertionError('Read-only diagnostic called '+name)))
        self.q=mock.MagicMock();self.q.__enter__.return_value=self.q
        self.stack.enter_context(mock.patch.object(agent,'Qga',return_value=self.q))

    def assert_unchanged(self):
        self.assertEqual(self.path.read_bytes(),self.before);self.assertEqual(self.path.stat().st_mtime_ns,self.before_mtime)

    def observe(self,**overrides):
        observed={name:{'status':'OK'} for name in ['services','listeners','manual_mode','diagnostics']}
        observed.update(overrides)
        self.q.execute.return_value={'returncode':0,'stdout':json.dumps(observed),'stderr':''}
        return agent.doctor(103)

    def test_doctor_reads_without_runtime_acceptance_or_state_writes(self):
        report=self.observe(manual_mode={'status':'OK','active':True})
        self.assertEqual(report['status'],'OK');self.assertTrue(report['read_only']);self.assertFalse(report['ui_input_issued'])
        self.assertTrue(report['checks']['manual_mode']['active'])
        self.assertEqual(report['checks']['recorded_verification']['results']['gpu']['runtime'],'PASS_GPU_RUNTIME')
        argv,timeout=self.q.execute.call_args.args
        self.assertEqual(argv[:2],['/usr/bin/python3','-c']);self.assertLessEqual(timeout,30)
        compile(argv[2],'<doctor>','exec')
        for forbidden in ['agent-mcp-acceptance','systemd-run','agent-set-mode','systemctl restart','apt-get','qm start']:
            self.assertNotIn(forbidden,argv[2])
        self.assert_unchanged()

    def test_doctor_keeps_unknown_checks_distinct_from_attention(self):
        report=self.observe(services={'status':'UNKNOWN','error':'user bus unavailable'})
        self.assertEqual(report['status'],'UNKNOWN');self.assertEqual(report['checks']['listeners']['status'],'OK')
        report=self.observe(listeners={'status':'WARN','non_loopback':['0.0.0.0:9222']})
        self.assertEqual(report['status'],'ATTENTION');self.assert_unchanged()

    def test_host_rules_and_current_audit_gap_are_readonly_diagnostics(self):
        self.state['config']={'network_policy':'isolated','mac':'52:54:00:00:00:01','bridge':'vmbr0'}
        self.owned.return_value={'net0':'virtio=52:54:00:00:00:01,bridge=vmbr0'}
        self.path.write_text(json.dumps(self.state))
        self.before=self.path.read_bytes();self.before_mtime=self.path.stat().st_mtime_ns
        for valid in [True,False]:
            with self.subTest(valid=valid),mock.patch.object(agent.network,'status',return_value=dict(rules_match=valid,boot_unit_present=True,config_present=True)), \
                 mock.patch.object(agent.audit,'status',return_value=dict(enabled=True,coverage_gap=None if valid else {'reason':'collector_stopped'})):
                report=self.observe()
            self.assertEqual(report['status'],'OK' if valid else 'ATTENTION')
            self.assert_unchanged()
        self.owned.return_value['net1']='virtio=52:54:00:00:00:02,bridge=vmbr0'
        report=self.observe()
        self.assertEqual(report['status'],'UNKNOWN');self.assertIn('extra VM NIC',report['checks']['network']['error'])

    def test_lost_qga_is_unknown_and_does_not_repair(self):
        self.q.execute.side_effect=TimeoutError('guest disconnected')
        report=agent.doctor(103)
        self.assertEqual(report['status'],'UNKNOWN')
        for name in ['services','listeners','manual_mode','diagnostics']:
            self.assertEqual(report['checks'][name]['status'],'UNKNOWN')
        self.assert_unchanged()

    def test_wrong_owner_does_not_contact_the_guest(self):
        self.owned.side_effect=RuntimeError('VMID now belongs to a different VM')
        report=agent.doctor(103)
        self.assertEqual(report['status'],'UNKNOWN');self.q.execute.assert_not_called();self.assert_unchanged()

    def test_incomplete_guest_observation_is_not_reported_healthy(self):
        report=self.observe(services=None)
        self.assertEqual(report['status'],'UNKNOWN');self.assertEqual(report['checks']['services']['status'],'UNKNOWN')
        self.assert_unchanged()

    def test_logs_read_only_a_bounded_tail_using_guest_file_calls(self):
        content=b'old line\n'+b'x'*17000+b'\nlatest line\n';cursor=[0]
        def call(name,args):
            if name=='guest-file-open':
                self.assertEqual(args['path'],str(Path('/var/lib/onepve-agent/jobs/install-1.log')));return 4
            if name=='guest-file-seek':
                cursor[0]=len(content) if args['whence']=='end' else args['offset'];return {'position':cursor[0]}
            if name=='guest-file-read':
                self.assertLessEqual(args['count'],16384)
                return {'buf-b64':base64.b64encode(content[cursor[0]:cursor[0]+args['count']]).decode()}
            if name=='guest-file-close':return {}
            self.fail(name)
        self.q.call.side_effect=call
        report=agent.logs(103)
        self.assertEqual(report['status'],'OK');self.assertTrue(report['truncated']);self.assertLessEqual(report['tail_bytes'],16384)
        self.assertEqual(report['tail'],'latest line\n');self.q.execute.assert_not_called();self.assert_unchanged()

    def test_logs_missing_job_or_unavailable_guest_does_not_create_work(self):
        self.assertEqual(agent.logs(103,'driver')['status'],'NOT_RECORDED')
        self.q.call.assert_not_called();self.owned.assert_not_called()
        self.q.call.side_effect=ConnectionResetError('disconnected')
        self.assertEqual(agent.logs(103)['status'],'UNKNOWN');self.assert_unchanged()

    def test_state_identity_is_checked_before_qga(self):
        self.path.write_text(json.dumps(dict(self.state,vmid=104)))
        with self.assertRaisesRegex(RuntimeError,'VMID differs'):agent.doctor(103)
        self.owned.assert_not_called();self.q.execute.assert_not_called()

    def test_cli_routes_diagnostics_without_verification(self):
        with contextlib.redirect_stdout(io.StringIO()),mock.patch.object(agent,'doctor',return_value={}) as doctor:
            self.assertEqual(agent.main(['doctor','--vmid','103']),0)
        doctor.assert_called_once_with(103)
        with contextlib.redirect_stdout(io.StringIO()),mock.patch.object(agent,'logs',return_value={}) as logs:
            self.assertEqual(agent.main(['logs','--vmid','103','--job','driver']),0)
        logs.assert_called_once_with(103,'driver')


if __name__=='__main__':unittest.main()
