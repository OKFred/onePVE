"""VM lifecycle integrates a host guard before enabling the first guest packet."""
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
import onepve_agent as agent
from test_agent import PUBLIC


def config(**changes):
    values=dict(vmid=103,storage='pool',bridge='vmbr0',ssh_key=PUBLIC,gpu='none',
                mac='BC:24:11:00:01:03',expected_ip='192.168.50.103',management_cidrs=['192.168.12.0/24'],
                nameserver='1.1.1.1',isolation_gateway='192.168.50.1',dhcp_server='192.168.50.1')
    values.update(changes)
    return agent.normalize(values)


def state(**changes):
    value=dict(vmid=103,owner='a'*32,operation='create',phase='READY_SOFTWARE',config=config(),
               artifacts={'image':{'path':'/cache/image.qcow2'}},resources={'disk_format':'qcow2','snippets_storage':'pool'},
               snippets={'network-hook.py':{'volume':'pool:snippets/network-hook.py'}},
               network={'managed':True,'protected_cidrs':['192.168.0.0/16'],'installed':{'table':'owned-table'}})
    value.update(changes)
    return value


class NetworkIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(agent,'AGENT_STATE',Path(self.temp.name)))
        self.stack.enter_context(mock.patch.object(agent,'require_root'))
        self.stack.enter_context(mock.patch.object(agent,'_lock',side_effect=lambda *args,**kwargs:contextlib.nullcontext()))
        # An omitted mock must fail before touching a real nft/systemd/VM resource.
        for namespace,names in [(agent,['run','_stop']), (agent.network,['run','install','remove','status']),
                                (agent.audit,['configure','remove','status'])]:
            for name in names:
                self.stack.enter_context(mock.patch.object(namespace,name,side_effect=AssertionError('Unexpected host access: '+name)))

    def write(self,value):
        path=agent._state_path(value['vmid']);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value));return path

    def live(self,value):
        return dict(net0='virtio='+value['config']['mac']+',bridge=vmbr0,link_down=0',
                    hookscript='pool:snippets/network-hook.py')

    def preflight(self):
        return dict(managed=True,policy='isolated',protected_cidrs=['192.168.0.0/16'])

    def test_new_default_is_isolated_and_audit_is_opt_in(self):
        value=config()
        self.assertEqual(value['network_policy'],'isolated');self.assertFalse(value['connection_audit'])
        self.assertEqual(config(network_policy='lan')['network_policy'],'lan')
        for field in ['expected_ip','management_cidrs','nameserver','dhcp_server','isolation_gateway','mac']:
            with self.subTest(field=field),self.assertRaises((RuntimeError,ValueError)):
                config(**{field:[] if field=='management_cidrs' else None})

    def test_static_isolation_uses_the_configured_gateway_without_dhcp(self):
        value=config(address='192.168.50.103/24',gateway='192.168.50.1',dhcp_server=None,isolation_gateway=None)
        self.assertEqual(value['expected_ip'],'192.168.50.103')

    def test_network_preflight_failure_precedes_download_create_and_delete(self):
        for action in ['plan','create','rebuild']:
            with self.subTest(action=action),contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(agent,'_exists',return_value=False))
                stack.enter_context(mock.patch.object(agent,'_previous_record',return_value=None))
                stack.enter_context(mock.patch.object(agent,'_resources',return_value={}))
                stack.enter_context(mock.patch.object(agent,'_select_gpu',return_value=None))
                stack.enter_context(mock.patch.object(agent,'_deletion_check',return_value={}))
                stack.enter_context(mock.patch.object(agent.network,'preflight',side_effect=RuntimeError('unsafe network')))
                forbidden=[stack.enter_context(mock.patch.object(agent,name)) for name in ['prepare','_bundle','_delete','_continue','_save']]
                images=stack.enter_context(mock.patch.object(agent.artifacts,'image_options'))
                with self.assertRaisesRegex(RuntimeError,'unsafe network'):
                    getattr(agent,action)(config(),103) if action=='rebuild' else getattr(agent,action)(config())
                for operation in forbidden:operation.assert_not_called()
                images.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).rglob('*')),[])

    def test_first_boot_is_disconnected_until_rules_hook_and_optional_audit(self):
        for enabled in [False,True]:
            with self.subTest(audit=enabled),contextlib.ExitStack() as stack:
                s=state(phase='VM_CREATING',config=config(connection_audit=enabled));current={};events=[]
                def execute(argv,**kwargs):
                    events.append(('command',list(argv)))
                    if argv[:2]==['qm','create']:
                        current.update(description='onePVE owner='+s['owner'],tags='onepve-agent',net0=argv[argv.index('--net0')+1])
                        self.assertIn('link_down=1',current['net0'])
                    elif argv[:3]==['qm','disk','import']:current['unused0']='pool:103/disk.qcow2'
                    elif argv[:2]==['qm','set']:
                        for index in range(3,len(argv),2):current[argv[index][2:]]=argv[index+1]
                        if '--scsi0' in argv:current.pop('unused0',None)
                        if '--hookscript' in argv:events.append(('hook',None))
                        if '--net0' in argv and 'link_down=0' in argv[-1]:events.append(('up',None))
                    elif argv[:2]==['qm','status']:return subprocess.CompletedProcess(argv,0,'status: stopped','')
                    elif argv[:2]==['qm','start']:
                        self.assertIn('link_down=0',current['net0']);self.assertIn('network-hook.py',current['hookscript'])
                        events.append(('start',None))
                    return subprocess.CompletedProcess(argv,0,'','')
                def install(value):
                    self.assertIn('link_down=1',current['net0']);events.append(('rules',None));return {'table':'owned-table'}
                def snippet(value,name,body,*args):
                    volume='pool:snippets/'+s['owner']+'-'+name
                    value['snippets'][name]={'volume':volume};return volume
                stack.enter_context(mock.patch.object(agent,'run',side_effect=execute))
                stack.enter_context(mock.patch.object(agent,'_exists',side_effect=lambda vmid:bool(current)))
                stack.enter_context(mock.patch.object(agent,'vm_config',side_effect=lambda vmid:dict(current)))
                stack.enter_context(mock.patch.object(agent,'_snippet',side_effect=snippet))
                stack.enter_context(mock.patch.object(agent,'_wait_cloud'))
                stack.enter_context(mock.patch.object(agent.network,'install',side_effect=install))
                stack.enter_context(mock.patch.object(agent.network,'status',return_value={'table_present':True,'rules_match':True}))
                collect=stack.enter_context(mock.patch.object(agent.audit,'configure',side_effect=lambda *args:events.append(('audit',None))))
                stack.enter_context(mock.patch.object(agent.audit,'status',return_value=dict(enabled=True,active=True,coverage_gap=None)))
                agent._create_vm(s)
                labels=[kind for kind,_ in events]
                self.assertLess(labels.index('rules'),labels.index('hook'));self.assertLess(labels.index('hook'),labels.index('up'))
                self.assertLess(labels.index('up'),labels.index('start'))
                if enabled:
                    self.assertLess(labels.index('hook'),labels.index('audit'));self.assertLess(labels.index('audit'),labels.index('up'))
                else:collect.assert_not_called()
                self.assertEqual(s['phase'],'CLOUD_READY')

    def test_active_boot_service_without_a_live_table_never_enables_the_nic(self):
        s=state();current=self.live(s);current['net0']=current['net0'].replace('link_down=0','link_down=1')
        with mock.patch.object(agent,'_owned',return_value=current),mock.patch.object(agent,'_snippet',return_value=current['hookscript']), \
             mock.patch.object(agent.network,'install',return_value={'service_active':True}), \
             mock.patch.object(agent.network,'status',return_value={'service_active':True,'table_present':False,'rules_match':True}), \
             mock.patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            with self.assertRaisesRegex(RuntimeError,'guard readback differs'):agent._network_ready(s)
        self.assertFalse(any('--net0' in call.args[0] for call in run.call_args_list))

    def test_optional_audit_requires_fresh_observation_before_network_release(self):
        for observed in [dict(enabled=True,active=False,coverage_gap={'reason':'collector_stopped'}),
                         dict(enabled=True,active=True,coverage_gap={'reason':'collector_heartbeat_stale'})]:
            with self.subTest(observed=observed),mock.patch.object(agent.audit,'configure'), \
                 mock.patch.object(agent.audit,'status',return_value=observed):
                with self.assertRaisesRegex(RuntimeError,'NIC remains disconnected'):
                    agent._audit_ready(state(),True,seconds=0)
        agent.run.assert_not_called()

    def test_first_boot_with_a_live_but_changed_ruleset_never_enables_the_nic(self):
        s=state();current=self.live(s);current['net0']=current['net0'].replace('link_down=0','link_down=1')
        with mock.patch.object(agent,'_owned',return_value=current),mock.patch.object(agent,'_snippet',return_value=current['hookscript']), \
             mock.patch.object(agent.network,'install',return_value={'service_active':True}), \
             mock.patch.object(agent.network,'status',return_value={'service_active':True,'table_present':True,'rules_match':False}), \
             mock.patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            with self.assertRaisesRegex(RuntimeError,'guard readback differs'):agent._network_ready(s)
        self.assertFalse(any('--net0' in call.args[0] for call in run.call_args_list))
        self.assertIn('link_down=1',current['net0'])

    def test_first_boot_guard_failure_keeps_guest_stopped_and_nic_down(self):
        for missing_table in [False,True]:
            with self.subTest(missing_table=missing_table):
                s=state(phase='CLOUD_WAIT');current=self.live(s)
                current.update(scsi0='pool:disk',cicustom='user=pool:snippets/'+s['owner'])
                current['net0']=current['net0'].replace('link_down=0','link_down=1')
                with mock.patch.object(agent,'_owned',return_value=current),mock.patch.object(agent,'_snippet',return_value=current['hookscript']), \
                     mock.patch.object(agent.network,'install',side_effect=(lambda value:{'table':'owned-table'}) if missing_table else RuntimeError('rules rejected')), \
                     mock.patch.object(agent.network,'status',return_value={'table_present':False,'rules_match':True}), \
                     mock.patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run, \
                     mock.patch.object(agent,'_wait_cloud') as wait:
                    with self.assertRaises(RuntimeError):agent._create_vm(s)
                self.assertFalse(any(call.args[0][:2]==['qm','start'] or '--net0' in call.args[0] for call in run.call_args_list))
                self.assertIn('link_down=1',current['net0']);wait.assert_not_called();self.assertEqual(s['phase'],'CLOUD_WAIT')

    def test_cloud_wait_resume_disconnects_an_already_enabled_nic_before_reinstall(self):
        s=state(phase='CLOUD_WAIT');current=self.live(s);calls=[]
        with mock.patch.object(agent,'_owned',return_value=current), \
             mock.patch.object(agent.network,'install',side_effect=RuntimeError('install failed')), \
             mock.patch.object(agent,'run',side_effect=lambda args,**kwargs:calls.append(args)):
            with self.assertRaisesRegex(RuntimeError,'install failed'):agent._network_ready(s)
        self.assertEqual(len(calls),1);self.assertIn('link_down=1',calls[0][-1])

    def test_legacy_resume_does_not_normalize_or_install_new_network_policy(self):
        cfg=dict(config(network_policy='legacy'));cfg.pop('network_policy')
        s=state(config=cfg);s.pop('network');path=self.write(s);before=path.read_bytes()
        with mock.patch.object(agent,'_validate_artifacts'),mock.patch.object(agent,'_continue') as continuation:
            answer=agent.resume(103)
        self.assertNotIn('network_policy',answer['config']);continuation.assert_not_called()
        agent.network.install.assert_not_called();self.assertEqual(path.read_bytes(),before)

    def test_gpu_detach_restores_network_hook_before_restarting(self):
        s=state(owned_gpu_bdf='0000:00:02.1');s['snippets']['gpu-hook.py']={'volume':'pool:snippets/gpu-hook.py'}
        current=self.live(s);current.update(hostpci0='0000:00:02.1,pcie=1',hookscript='pool:snippets/gpu-hook.py')
        calls=[]
        with mock.patch.object(agent,'_owned',return_value=current),mock.patch.object(agent,'_stop'), \
             mock.patch.object(agent,'_snippet',return_value='pool:snippets/network-hook.py'),mock.patch.object(agent,'_wait_cloud'), \
             mock.patch.object(agent,'run',side_effect=lambda argv,**kwargs:calls.append(argv)):
            agent._detach_gpu(s)
        hook=next(i for i,argv in enumerate(calls) if '--hookscript' in argv)
        start=next(i for i,argv in enumerate(calls) if argv[:2]==['qm','start'])
        self.assertLess(hook,start);self.assertEqual(calls[hook][-1],'pool:snippets/network-hook.py')

    def test_interrupted_gpu_detach_recovers_hook_without_repeating_pci_deletion(self):
        s=state(owned_gpu_bdf='0000:00:02.1');s['snippets']['gpu-hook.py']={'volume':'pool:snippets/gpu-hook.py'}
        current=self.live(s);current.pop('hookscript');calls=[]
        with mock.patch.object(agent,'_owned',return_value=current),mock.patch.object(agent,'_stop'), \
             mock.patch.object(agent,'_snippet',return_value='pool:snippets/network-hook.py'),mock.patch.object(agent,'_wait_cloud'), \
             mock.patch.object(agent,'run',side_effect=lambda argv,**kwargs:calls.append(argv)):
            agent._detach_gpu(s)
        self.assertFalse(any('--delete' in argv for argv in calls))
        self.assertEqual(calls,[['qm','set','103','--hookscript','pool:snippets/network-hook.py'],['qm','start','103']])

    def test_configure_failure_keeps_nic_disconnected_and_pending_intent(self):
        for failure in ['install','audit','verify','linkup']:
            with self.subTest(failure=failure),contextlib.ExitStack() as stack:
                s=state();s['config']['connection_audit']=failure=='audit';path=self.write(s);current=self.live(s);calls=[]
                def execute(argv,**kwargs):
                    calls.append(argv)
                    if '--net0' in argv:
                        if failure=='linkup' and 'link_down=0' in argv[-1]:raise TimeoutError('link outcome uncertain')
                        current['net0']=argv[-1]
                    return subprocess.CompletedProcess(argv,0,'','')
                stack.enter_context(mock.patch.object(agent,'_owned',return_value=current))
                stack.enter_context(mock.patch.object(agent.network,'preflight',return_value=self.preflight()))
                stack.enter_context(mock.patch.object(agent.network,'install',side_effect=RuntimeError('install failed') if failure=='install' else lambda value:{'table':'owned-table'}))
                stack.enter_context(mock.patch.object(agent.network,'status',return_value={'table_present':failure!='verify','rules_match':True}))
                stack.enter_context(mock.patch.object(agent.audit,'configure',side_effect=RuntimeError('audit failed') if failure=='audit' else lambda *args:{}))
                stack.enter_context(mock.patch.object(agent,'run',side_effect=execute))
                with self.assertRaises((RuntimeError,TimeoutError)):agent.configure_network(103,{'network_policy':'lan'})
                self.assertIn('link_down=1',current['net0'])
                persisted=json.loads(path.read_text());self.assertIsNotNone(persisted.get('network_pending'))
                self.assertEqual(persisted['network_pending']['config']['network_policy'],'lan')

    def test_policy_update_with_changed_rule_fingerprint_keeps_nic_down_and_pending(self):
        s=state();path=self.write(s);current=self.live(s);calls=[]
        def execute(argv,**kwargs):
            calls.append(argv)
            if '--net0' in argv:current['net0']=argv[-1]
            return subprocess.CompletedProcess(argv,0,'','')
        with mock.patch.object(agent,'_owned',return_value=current), \
             mock.patch.object(agent.network,'preflight',return_value=self.preflight()), \
             mock.patch.object(agent.network,'install',return_value={'service_active':True}), \
             mock.patch.object(agent.network,'status',return_value={'service_active':True,'table_present':True,'rules_match':False}), \
             mock.patch.object(agent,'run',side_effect=execute):
            with self.assertRaisesRegex(RuntimeError,'guard readback differs'):
                agent.configure_network(103,{'network_policy':'lan'})
        self.assertIn('link_down=1',current['net0']);self.assertIsNotNone(json.loads(path.read_text()).get('network_pending'))
        self.assertFalse(any('--net0' in argv and 'link_down=0' in argv[-1] for argv in calls))

    def test_successful_policy_update_clears_pending_only_after_verified_linkup(self):
        s=state();path=self.write(s);current=self.live(s);events=[]
        def execute(argv,**kwargs):
            if '--net0' in argv:
                pending=json.loads(path.read_text()).get('network_pending')
                self.assertIsNotNone(pending)
                current['net0']=argv[-1];events.append('up' if 'link_down=0' in argv[-1] else 'down')
            return subprocess.CompletedProcess(argv,0,'','')
        def installed(value):
            self.assertIn('link_down=1',current['net0']);events.append('rules');return {'table':'owned-table'}
        def verified(value):
            events.append('verify');return {'table_present':True,'rules_match':True}
        with mock.patch.object(agent,'_owned',return_value=current), \
             mock.patch.object(agent.network,'preflight',return_value=self.preflight()), \
             mock.patch.object(agent.network,'install',side_effect=installed), \
             mock.patch.object(agent.network,'status',side_effect=verified),mock.patch.object(agent.audit,'status',return_value={'enabled':False}), \
             mock.patch.object(agent,'run',side_effect=execute):
            answer=agent.configure_network(103,{'network_policy':'lan'})
        self.assertEqual(answer['status'],'NETWORK_CONFIGURED');self.assertIsNone(json.loads(path.read_text())['network_pending'])
        self.assertLess(events.index('down'),events.index('rules'));self.assertLess(events.index('rules'),events.index('verify'))
        self.assertLess(events.index('verify'),events.index('up'))

    def test_configure_legacy_or_foreign_hook_refuses_before_nic_changes(self):
        for legacy in [True,False]:
            with self.subTest(legacy=legacy):
                s=state(config=config(network_policy='legacy') if legacy else config());self.write(s);current=self.live(s)
                if not legacy:current['hookscript']='external:snippets/foreign-hook'
                with mock.patch.object(agent,'_owned',return_value=current), \
                     mock.patch.object(agent.network,'preflight',return_value=self.preflight()):
                    with self.assertRaisesRegex(RuntimeError,'Legacy|externally'):agent.configure_network(103,{'network_policy':'lan'})
                agent.run.assert_not_called();agent.network.install.assert_not_called()

    def test_configure_dry_run_is_read_only(self):
        s=state();path=self.write(s);before=path.read_bytes()
        with mock.patch.object(agent,'_owned',return_value=self.live(s)), \
             mock.patch.object(agent.network,'preflight',return_value=self.preflight()):
            report=agent.configure_network(103,{'network_policy':'lan'},True)
        self.assertEqual(report['status'],'PLAN_ONLY');self.assertEqual(path.read_bytes(),before)
        agent.run.assert_not_called();agent.network.install.assert_not_called()

    def test_policy_update_rejects_an_unguarded_extra_nic_or_changed_nic_identity(self):
        changes=[{'net1':'virtio=BC:24:11:00:01:04,bridge=vmbr0'},
                 {'net0':'virtio=BC:24:11:00:01:04,bridge=vmbr0'},
                 {'net0':'virtio=BC:24:11:00:01:03,bridge=vmbr1'},
                 {'net0':'virtio=BC:24:11:00:01:03,bridge=vmbr0,tag=99'},
                 {'net0':'virtio=BC:24:11:00:01:03,bridge=vmbr0,trunks=1;2'}]
        for change in changes:
            with self.subTest(change=change):
                s=state();path=self.write(s);before=path.read_bytes();current=self.live(s);current.update(change)
                with mock.patch.object(agent,'_owned',return_value=current), \
                     mock.patch.object(agent.network,'preflight',return_value=self.preflight()):
                    with self.assertRaises(RuntimeError):agent.configure_network(103,{'network_policy':'isolated'})
                self.assertEqual(path.read_bytes(),before);agent.run.assert_not_called();agent.network.install.assert_not_called()

    def test_remove_uses_only_the_target_owner_and_skips_legacy_resources(self):
        s=state();foreign=state(vmid=104,owner='b'*32);self.write(s);other_path=self.write(foreign);before=other_path.read_bytes()
        with mock.patch.object(agent,'_deletion_check',return_value={}),mock.patch.object(agent,'_owned'), \
             mock.patch.object(agent,'_delete'),mock.patch.object(agent,'_remove_snippets'), \
             mock.patch.object(agent.network,'remove') as remove,mock.patch.object(agent.audit,'remove') as audit_remove:
            agent.remove(103,103)
        self.assertEqual(remove.call_args.args[0]['owner'],'a'*32);self.assertEqual(remove.call_count,1)
        self.assertEqual(audit_remove.call_args.args[0]['vmid'],103);self.assertEqual(other_path.read_bytes(),before)
        agent._remove_network(state(config=config(network_policy='legacy')))

    def test_foreign_vm_ownership_blocks_network_removal(self):
        self.write(state())
        with mock.patch.object(agent,'_deletion_check',return_value={}), \
             mock.patch.object(agent,'_owned',side_effect=RuntimeError('different/unmanaged VM')),mock.patch.object(agent,'_delete') as delete:
            with self.assertRaisesRegex(RuntimeError,'different/unmanaged'):agent.remove(103,103)
        delete.assert_not_called();agent.network.remove.assert_not_called();agent.audit.remove.assert_not_called()


if __name__=='__main__':unittest.main()
