import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import struct
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
import onepve_agent as agent


def wire(*fields):
    return base64.b64encode(b''.join(struct.pack('>I',len(f))+f for f in fields)).decode()


PUBLIC='ssh-ed25519 '+wire(b'ssh-ed25519',bytes(range(32)))+' acceptance'


def config(**changes):
    value=dict(vmid=103,storage='agent-browser',bridge='vmbr0',ssh_key=PUBLIC,gpu='none')
    value.update(changes);return agent.normalize(value)


def state(**changes):
    value=dict(vmid=103,owner='a'*32,operation='create',phase='BASE_READY',config=config(),
               resources={'snippets_storage':'agent-browser'},snippets={},
               guest_stage='/opt/onepve-stage/'+'a'*32,guest_payload='/opt/onepve-stage/'+'a'*32+'/payload',
               selected_gpu=dict(bdf='0000:00:02.1',vendor='0x8086',device='0x7d67'))
    value.update(changes);return value


def result(stdout='',returncode=0,stderr=''):
    return dict(returncode=returncode,stdout=stdout,stderr=stderr)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.stack=contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(agent,'require_root'))
        self.stack.enter_context(mock.patch.object(agent,'AGENT_STATE',self.root/'state'))
        self.stack.enter_context(mock.patch.object(agent,'VM_CONFIGS',self.root/'vms'))
        self.stack.enter_context(mock.patch.object(agent,'PVE_NODES',self.root/'nodes'))
        (self.root/'vms').mkdir()

    def tearDown(self):
        self.stack.close();self.temp.cleanup()

    def test_public_key_wire_and_types(self):
        self.assertEqual(agent.public_key(PUBLIC),PUBLIC)
        ecdsa='ecdsa-sha2-nistp521 '+wire(b'ecdsa-sha2-nistp521',b'nistp521',b'\x04'+bytes(132))
        self.assertEqual(agent.public_key(ecdsa),ecdsa)
        bad=['-----BEGIN OPENSSH PRIVATE KEY-----',PUBLIC+'\n',
             'ssh-rsa '+wire(b'ssh-ed25519',bytes(32)),
             'ssh-ed25519 '+wire(b'ssh-ed25519',bytes(31)),
             'ssh-ed25519 '+base64.b64encode(struct.pack('>I',2**32-1)+b'x').decode(),
             PUBLIC.split()[0]+' '+wire(b'ssh-ed25519',bytes(32),b'extra')]
        for key in bad:
            with self.subTest(key=key[:40]),self.assertRaises(RuntimeError):agent.public_key(key)

    def test_minimum_resources_and_no_shell_values(self):
        self.assertEqual(config()['memory'],8192)
        self.assertEqual(config()['disk'],32)
        self.assertEqual(config(disk=32)['disk'],32)
        self.assertEqual(config(disk=64)['disk'],64)
        for values in [dict(cores=3),dict(memory=8191),dict(disk=31),dict(vmid=99),dict(storage='x;reboot'),dict(bridge='../vmbr0'),dict(mac='FF:00:00:00:00:01')]:
            with self.subTest(values=values),self.assertRaises(RuntimeError):config(**values)

    def test_static_network_is_validated_and_bound_to_expected_address(self):
        value=config(address='192.168.11.144/24',gateway='192.168.11.1',nameserver='192.168.11.1')
        self.assertEqual(value['address'],'192.168.11.144/24');self.assertEqual(value['expected_ip'],'192.168.11.144')
        cases=[dict(address='192.168.11.144'),dict(address='192.168.11.0/24'),dict(address='192.168.11.255/24'),
               dict(address='192.168.11.144/24',gateway='192.168.12.1'),dict(address='192.168.11.144/24',gateway='192.168.11.144'),
               dict(address='192.168.11.144/24',gateway='192.168.11.255'),dict(address='192.168.11.144/24',gateway='192.168.11.1',expected_ip='192.168.11.147'),
               dict(address='::1/64',gateway='192.168.11.1'),dict(address='127.0.0.2/8',gateway='127.0.0.1'),dict(gateway='192.168.11.1'),
               dict(nameserver='8.8.8.8 1.1.1.1'),dict(nameserver='0.0.0.0'),dict(gateway='192.168.11.1;reboot')]
        for fields in cases:
            with self.subTest(fields=fields),self.assertRaises(RuntimeError):config(**fields)
        self.assertIsNone(config()['address']);self.assertIsNone(config()['gateway'])
        self.assertEqual(config(nameserver='1.1.1.1')['nameserver'],'1.1.1.1')

    def test_create_collision_rejects_before_artifacts_or_write(self):
        with mock.patch.object(agent,'_exists',return_value=True),mock.patch.object(agent,'_lock',return_value=contextlib.nullcontext()), \
             mock.patch.object(agent,'prepare') as prepare,mock.patch.object(agent,'_save') as save:
            with self.assertRaisesRegex(RuntimeError,'already exists'):agent.create(config())
        prepare.assert_not_called();save.assert_not_called()

    def test_dry_run_has_zero_mutations_and_no_cache(self):
        with mock.patch.object(agent,'_exists',return_value=False),mock.patch.object(agent,'_resources',return_value={}), \
             mock.patch.object(agent.artifacts,'resolve_image',return_value={'sha512':'pinned'}),mock.patch.object(agent,'run') as run, \
             mock.patch.object(agent,'_save') as save,mock.patch.object(agent,'prepare') as prepare:
            answer=agent.create(config(),True)
        self.assertEqual(answer['status'],'PLAN_ONLY');run.assert_not_called();save.assert_not_called();prepare.assert_not_called()
        self.assertFalse((self.root/'state').exists())

    def test_explicit_confirmation_precedes_deletion(self):
        with mock.patch.object(agent,'_deletion_check') as check:
            for action in [lambda:agent.remove(103,104),lambda:agent.rebuild(config(),104)]:
                with self.assertRaisesRegex(RuntimeError,'confirm|confirmation'):action()
        check.assert_not_called()

    def test_shared_disk_in_other_snapshot_rejects_delete(self):
        (self.root/'vms'/'103.conf').write_text('scsi0: pool:vm-103-disk-0\n')
        (self.root/'vms'/'104.conf').write_text('name: other\n[snap]\nscsi0: pool:vm-103-disk-0\n')
        with mock.patch.object(agent,'_raw_config',return_value='scsi0: pool:vm-103-disk-0\n'), \
             mock.patch.object(agent,'vm_config',return_value={'name':'old'}),mock.patch.object(agent,'node_name',return_value='pve'), \
             mock.patch.object(agent,'pve_get',return_value=[{'volid':'pool:vm-103-disk-0'}]), \
             mock.patch.object(agent,'_volume_identity',return_value={'identity':['file',1,2],'path':'/mock/target'}):
            with self.assertRaisesRegex(RuntimeError,'Other VM'):agent._deletion_check(103)

    def test_direct_host_disk_cannot_be_destroyed(self):
        with mock.patch.object(agent,'_exists',return_value=True),mock.patch.object(agent,'_raw_config',return_value='scsi0: /dev/disk/by-id/real-disk\n'),mock.patch.object(agent,'vm_config',return_value={}):
            with self.assertRaisesRegex(RuntimeError,'Direct host disk'):agent._deletion_check(103)

    def test_shared_disk_on_another_cluster_node_rejects_delete(self):
        remote=self.root/'nodes'/'other-node'/'qemu-server';remote.mkdir(parents=True)
        (remote/'105.conf').write_text('scsi0: shared:vm-103-disk-0\n')
        with mock.patch.object(agent,'_exists',return_value=True),mock.patch.object(agent,'_raw_config',return_value='scsi0: shared:vm-103-disk-0\n'), \
             mock.patch.object(agent,'vm_config',return_value={}),mock.patch.object(agent,'node_name',return_value='pve'), \
             mock.patch.object(agent,'pve_get',return_value=[{'volid':'shared:vm-103-disk-0'}]), \
             mock.patch.object(agent,'_volume_identity',return_value={'identity':['file',1,2],'path':'/mock/target'}):
            with self.assertRaisesRegex(RuntimeError,'Other VM'):agent._deletion_check(103)

    def test_delete_refuses_configuration_drift(self):
        s=state(delete_guard={'config_sha256':hashlib.sha256(b'old').hexdigest()})
        with mock.patch.object(agent,'_raw_config',return_value='changed'),mock.patch.object(agent,'run') as run,mock.patch.object(agent,'_save') as save:
            with self.assertRaisesRegex(RuntimeError,'changed'):agent._delete(s)
        run.assert_not_called();save.assert_not_called()

    def test_ownership_checks_uuid_and_tag(self):
        with mock.patch.object(agent,'_exists',return_value=True),mock.patch.object(agent,'vm_config',return_value={'description':'foreign','tags':'onepve-agent'}):
            with self.assertRaisesRegex(RuntimeError,'different'):agent._owned(state())

    def test_rebuild_prepares_before_any_deletion(self):
        events=[]
        with mock.patch.object(agent,'_lock',return_value=contextlib.nullcontext()), \
             mock.patch.object(agent,'_resources',return_value={}),mock.patch.object(agent,'_select_gpu',return_value=None), \
             mock.patch.object(agent,'_deletion_check',return_value={}),mock.patch.object(agent,'_validate_artifacts'), \
             mock.patch.object(agent,'prepare',side_effect=lambda:(events.append('prepare') or {})), \
             mock.patch.object(agent,'_bundle',side_effect=lambda s:events.append('bundle')), \
             mock.patch.object(agent,'_delete',side_effect=lambda s:events.append('delete')),mock.patch.object(agent,'_continue',return_value={}):
            agent.rebuild(config(),103)
        self.assertLess(events.index('prepare'),events.index('delete'));self.assertLess(events.index('bundle'),events.index('delete'))

    def test_failed_artifact_prepare_never_deletes(self):
        with mock.patch.object(agent,'_lock',return_value=contextlib.nullcontext()),mock.patch.object(agent,'_resources',return_value={}), \
             mock.patch.object(agent,'_select_gpu',return_value=None),mock.patch.object(agent,'_deletion_check',return_value={}), \
             mock.patch.object(agent,'prepare',side_effect=RuntimeError('hash mismatch')),mock.patch.object(agent,'_delete') as delete:
            with self.assertRaisesRegex(RuntimeError,'hash mismatch'):agent.rebuild(config(),103)
        delete.assert_not_called()

    def test_resume_does_not_repeat_uncertain_deletion(self):
        with mock.patch.object(agent,'_exists',return_value=True),mock.patch.object(agent,'run') as run,mock.patch.object(agent,'_save') as save:
            with self.assertRaisesRegex(RuntimeError,'never repeats deletion'):agent._continue(state(phase='DELETING'))
        run.assert_not_called();save.assert_not_called()

    def test_job_observes_existing_success_without_duplicate_launch(self):
        s=state();q=mock.MagicMock();q.__enter__.return_value=q
        q.execute.return_value=result('LoadState=loaded\nActiveState=active\n');q.read.return_value=b'{"status":"SUCCESS","returncode":0}'
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'):
            agent._job(s,'install',['/bin/true']);agent._job(s,'install',['/bin/true'])
        q.write.assert_not_called();self.assertEqual(q.execute.call_count,1)

    def test_job_missing_result_waits_then_returns_and_script_is_valid(self):
        s=state();q=mock.MagicMock();q.__enter__.return_value=q
        q.execute.side_effect=[result('LoadState=not-found\n'),result(returncode=1),result()]
        q.read.side_effect=[RuntimeError('Guest Agent file not found'),b'{"status":"SUCCESS"}']
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'),mock.patch.object(agent.time,'sleep'):
            agent._job(s,'install',['/bin/true'])
        self.assertEqual(q.write.call_count,1);compile(q.write.call_args.args[1],'<job>','exec')
        launches=[x for x in q.execute.call_args_list if x.args[0][0]=='/usr/bin/systemd-run']
        self.assertEqual(len(launches),1);self.assertIn('--property=TimeoutStartSec=infinity',launches[0].args[0])
        self.assertIn('--no-block',launches[0].args[0]);self.assertEqual(launches[0].args[1],30)

    def test_failed_job_records_diagnostic_and_requires_explicit_retry(self):
        s=state();q=mock.MagicMock();q.__enter__.return_value=q
        q.execute.return_value=result('LoadState=loaded');q.read.return_value=b'{"status":"FAILED","log":"/guest/failure.log"}'
        with mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'):
            with self.assertRaisesRegex(RuntimeError,'failure.log'):agent._job(s,'driver',['/bin/false'])
            with self.assertRaisesRegex(RuntimeError,'explicit resume'):agent._job(s,'driver',['/bin/false'])

    def test_runtime_acceptance_parses_pretty_json_as_normal_user(self):
        s=state();answers=[result(json.dumps({'status':'PASS_BASE_RUNTIME'},indent=2)),result(json.dumps({'status':'passed'},indent=2))]
        with mock.patch.object(agent,'_guest',side_effect=answers) as guest,mock.patch.object(agent,'_save'):
            agent._runtime_verify(s)
        self.assertEqual(guest.call_args_list[1].args[1][:4],['/usr/sbin/runuser','-u','agent','--'])

    def test_generated_driver_and_prestart_scripts_compile(self):
        s=state(driver_spec=agent.gpu.driver_spec('6.12.113+deb13-amd64'))
        compile(agent._driver_script(s),'<driver>','exec');compile(agent._gpu_hook(s),'<prestart>','exec')
        self.assertIn("'-k',kernel,'-j','2'",agent._driver_script(s))
        for prerequisite in ['onepve-agent-firmware.sources','trixie-security','Components: non-free-firmware','debian-archive-keyring.gpg',"release.get('VERSION_ID'"]:
            self.assertIn(prerequisite,agent._driver_script(s))
        self.assertNotIn('pve-guests',agent._gpu_hook(s));self.assertIn("'0000:00:02.1'",agent._gpu_hook(s))

    def test_gpu_verification_cannot_accept_base_only_proof(self):
        with mock.patch.object(agent,'_guest',return_value=result(json.dumps({'status':'PASS_BASE_RUNTIME'}))):
            with self.assertRaisesRegex(RuntimeError,'Requested runtime'):agent._runtime_verify(state(),True)

    def test_successful_gpu_retry_archives_old_error_and_keeps_diagnostics(self):
        failure='driver failed; inspect guest /var/lib/onepve-agent/jobs/driver-attempt-1.log'
        s=state(phase='GPU_VERIFYING',gpu_error=failure,gpu_error_utc='2026-11-02T01:02:03+00:00',
                gpu_failure_history=[{'error':'older failure'}],job_history=[{'path':'/guest/failed-result.json'}])
        with mock.patch.object(agent,'_owned'),mock.patch.object(agent,'_runtime_verify'),mock.patch.object(agent,'_connection'):
            agent._continue(s)
        self.assertEqual(s['phase'],'READY_GPU');self.assertNotIn('gpu_error',s);self.assertNotIn('gpu_error_utc',s)
        archived=s['gpu_failure_history'][-1]
        self.assertEqual(archived['error'],failure);self.assertEqual(archived['log'],'/var/lib/onepve-agent/jobs/driver-attempt-1.log')
        self.assertEqual(archived['failed_utc'],'2026-11-02T01:02:03+00:00');self.assertIn('archived_utc',archived)
        self.assertEqual(s['job_history'],[{'path':'/guest/failed-result.json'}]);self.assertEqual(s['gpu_failure_history'][0],{'error':'older failure'})
        agent._archive_gpu_error(s);self.assertEqual(len(s['gpu_failure_history']),2)

    def test_legacy_gpu_error_keeps_text_without_inventing_failure_timestamp(self):
        s=state(gpu_error='legacy GuC failure');agent._archive_gpu_error(s)
        self.assertEqual(s['gpu_failure_history'][0]['error'],'legacy GuC failure')
        self.assertNotIn('failed_utc',s['gpu_failure_history'][0]);self.assertNotIn('gpu_error',s)

    def test_gpu_fallback_removes_only_its_target_assignment(self):
        s=state(owned_gpu_bdf='0000:00:02.1',snippets={'gpu-hook.py':{'volume':'pool:snippets/ours'}})
        with mock.patch.object(agent,'_owned',return_value={'hostpci0':'0000:00:02.1,pcie=1','hookscript':'pool:snippets/ours'}), \
             mock.patch.object(agent,'_stop') as stop,mock.patch.object(agent,'run') as run,mock.patch.object(agent,'_wait_cloud'), \
             mock.patch.object(agent,'_guest'),mock.patch.object(agent,'_wait_desktop') as desktop,mock.patch.object(agent,'_runtime_verify'),mock.patch.object(agent,'_save'):
            agent._fallback(s,RuntimeError('GuC failed'))
        stop.assert_called_once_with(103)
        desktop.assert_called_once_with(s)
        self.assertEqual(run.call_args_list[0].args[0],['qm','set','103','--delete','hostpci0,hookscript'])
        for call in run.call_args_list:self.assertNotIn('modprobe',call.args[0]);self.assertNotIn('reboot',call.args[0])

    def test_gpu_fallback_refuses_foreign_assignment_or_hook(self):
        s=state(owned_gpu_bdf='0000:00:02.1')
        for value in [{'hostpci0':'0000:00:03.0'},{'hostpci0':'0000:00:02.1','hookscript':'foreign'}]:
            with mock.patch.object(agent,'_owned',return_value=value),mock.patch.object(agent,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'edited outside'):agent._detach_gpu(s)
            run.assert_not_called()

    def test_remove_snippet_preserves_foreign_edit(self):
        path=self.root/'owned';path.write_text('edited')
        s=state(snippets={'user.yaml':{'path':str(path),'sha256':hashlib.sha256(b'old').hexdigest()}})
        with self.assertRaisesRegex(RuntimeError,'changed'):agent._remove_snippets(s)
        self.assertTrue(path.exists())

    def test_expected_address_failure_does_not_change_network(self):
        s=state(config=config(expected_ip='192.168.11.144'))
        with mock.patch.object(agent,'_guest',return_value=result(json.dumps({'ipv4':['192.168.11.200']}))),mock.patch.object(agent,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'differs'):agent._connection(s)
        run.assert_not_called()

    def test_cloud_init_has_no_password_or_private_key(self):
        content=agent._cloud_config(state());document=json.loads(content.split('\n',1)[1])
        self.assertFalse(document['ssh_pwauth']);self.assertTrue(document['disable_root']);self.assertTrue(document['users'][0]['lock_passwd'])
        self.assertEqual(document['users'][0]['ssh_authorized_keys'],[PUBLIC]);self.assertNotIn('passwd',document['users'][0])
        self.assertIsNone(document['users'][0]['sudo'])

    def test_cloud_init_uses_https_deb822_and_bounds_guest_apt_stalls(self):
        document=json.loads(agent._cloud_config(state()).split('\n',1)[1]);apt=document['apt']
        self.assertFalse(apt['preserve_sources_list']);self.assertNotIn('mirror+file:',apt['sources_list']);self.assertNotIn('http://',apt['sources_list'])
        stanzas=[dict(line.split(': ',1) for line in stanza.splitlines()) for stanza in apt['sources_list'].strip().split('\n\n')]
        self.assertEqual([s['Suites'] for s in stanzas],['trixie trixie-updates','trixie-security'])
        for stanza in stanzas:
            self.assertEqual(stanza['Components'],'main');self.assertEqual(stanza['Signed-By'],'/usr/share/keyrings/debian-archive-keyring.gpg')
        setting=document['write_files'][0]
        self.assertTrue(setting['path'].startswith('/etc/apt/apt.conf.d/'));self.assertEqual(setting['owner'],'root:root');self.assertEqual(setting['permissions'],'0644')
        for bound in ['Acquire::http::Timeout "15";','Acquire::https::Timeout "15";','Acquire::Retries "2";']:self.assertIn(bound,setting['content'])

    def test_cli_accepts_static_network_fields_without_shell_split(self):
        with mock.patch.object(agent,'plan',return_value={'status':'PLAN_ONLY'}) as plan,contextlib.redirect_stdout(io.StringIO()):
            agent.main(['plan','--vmid','103','--storage','pool','--bridge','vmbr0','--ssh-key',PUBLIC,
                        '--address','192.168.11.144/24','--gateway','192.168.11.1','--nameserver','192.168.11.1'])
        value=plan.call_args.args[0]
        self.assertEqual(value['disk'],32)
        self.assertEqual(value['address'],'192.168.11.144/24');self.assertEqual(value['gateway'],'192.168.11.1');self.assertEqual(value['nameserver'],'192.168.11.1')

    def disk_context(self,raw,owned,paths):
        stack=contextlib.ExitStack()
        stack.enter_context(mock.patch.object(agent,'_exists',return_value=True))
        stack.enter_context(mock.patch.object(agent,'_raw_config',return_value=raw))
        stack.enter_context(mock.patch.object(agent,'vm_config',return_value={}))
        stack.enter_context(mock.patch.object(agent,'node_name',return_value='pve'))
        stack.enter_context(mock.patch.object(agent,'pve_get',return_value=owned))
        def storage_path(argv,**kwargs):
            path=paths.get(argv[-1]);return subprocess.CompletedProcess(argv,0 if path else 1,str(path or ''),'')
        stack.enter_context(mock.patch.object(agent,'run',side_effect=storage_path))
        return stack

    def test_canonical_raw_path_alias_prevents_delete(self):
        disk=self.root/'disk.qcow2';disk.write_bytes(b'disk');alias=self.root/'alias.qcow2';alias.hardlink_to(disk)
        (self.root/'vms'/'104.conf').write_text('scsi0: '+alias.as_posix()+'\n')
        with self.disk_context('scsi0: pool:103/disk.qcow2\n',[{'volid':'pool:103/disk.qcow2'}],{'pool:103/disk.qcow2':disk}):
            with self.assertRaisesRegex(RuntimeError,'Other VM'):agent._deletion_check(103)

    def test_cloudinit_cdrom_alias_is_protected(self):
        cloud=self.root/'cloudinit.qcow2';cloud.write_bytes(b'cloudinit')
        (self.root/'vms'/'104.conf').write_text('ide2: '+cloud.as_posix()+',media=cdrom\n')
        with self.disk_context('ide2: pool:103/vm-103-cloudinit.qcow2,media=cdrom\n',
                              [{'volid':'pool:103/vm-103-cloudinit.qcow2','vmid':103}],{'pool:103/vm-103-cloudinit.qcow2':cloud}):
            with self.assertRaisesRegex(RuntimeError,'Other VM'):agent._deletion_check(103)

    def test_common_readonly_iso_is_not_owned_by_target_delete(self):
        disk=self.root/'disk';disk.write_bytes(b'disk');iso=self.root/'installer.iso';iso.write_bytes(b'iso')
        (self.root/'vms'/'104.conf').write_text('ide2: local:iso/shared.iso,media=cdrom\n')
        raw='scsi0: pool:103/disk\nide2: local:iso/shared.iso,media=cdrom\n'
        owned=[{'volid':'pool:103/disk','vmid':103,'content':'images'}, {'volid':'local:iso/shared.iso','content':'iso'}]
        with self.disk_context(raw,owned,{'pool:103/disk':disk,'local:iso/shared.iso':iso}):answer=agent._deletion_check(103)
        self.assertEqual(answer['volumes'],['pool:103/disk'])

    def test_unresolved_writable_alias_fails_closed(self):
        disk=self.root/'disk';disk.write_bytes(b'disk');(self.root/'vms'/'104.conf').write_text('scsi0: unknown:opaque-block\n')
        with self.disk_context('scsi0: pool:103/disk\n',[{'volid':'pool:103/disk'}],{'pool:103/disk':disk}):
            with self.assertRaisesRegex(RuntimeError,'Unresolved writable'):agent._deletion_check(103)

    def test_delete_rejects_replaced_disk_with_unchanged_configuration(self):
        disk=self.root/'disk';disk.write_bytes(b'old')
        with self.disk_context('scsi0: pool:103/disk\n',[{'volid':'pool:103/disk'}],{'pool:103/disk':disk}):
            guard=agent._deletion_check(103)
            disk.replace(self.root/'old-disk');disk.write_bytes(b'replacement')
            with mock.patch.object(agent,'_stop') as stop,mock.patch.object(agent,'_save') as save:
                with self.assertRaisesRegex(RuntimeError,'identities changed'):agent._delete(state(delete_guard=guard))
        stop.assert_not_called();save.assert_not_called()

    def lvm_report(self):
        return dict(vg_name='nvme970-2tb',lv_name='vm-300-disk-2',
                    vg_uuid='abcd12-EFGH-5678-ijkl-9012-mnop-QRSTuv',
                    lv_uuid='xYCCn6-gFQ9-mw5l-0I1A-a5XX-fJcc-6CMlZ4',
                    lv_path='/dev/nvme970-2tb/vm-300-disk-2',lv_dm_path='',
                    lv_kernel_major='-1',lv_kernel_minor='-1')

    def test_inactive_lvm_and_escaped_mapper_have_same_uuid_without_activation(self):
        row=self.lvm_report();report=json.dumps({'report':[{'lv':[row]}]})
        with mock.patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,report,'')) as run:
            first=agent._lvm_identity(PurePosixPath(row['lv_path']),None)
            second=agent._lvm_identity(PurePosixPath('/dev/mapper/nvme970--2tb-vm--300--disk--2'),None)
        self.assertEqual(first,second);self.assertEqual(first['identity'],['lvm',row['vg_uuid'],row['lv_uuid']])
        for call in run.call_args_list:
            self.assertEqual(call.args[0][0],'lvs');self.assertIn('--readonly',call.args[0]);self.assertNotIn('lvchange',call.args[0])

    def test_active_lvm_dm_node_uses_same_uuid_as_inactive_alias(self):
        row=self.lvm_report();report=json.dumps({'report':[{'lv':[row]}]})
        info=types.SimpleNamespace(st_mode=stat.S_IFBLK|0o600,st_rdev=42)
        device_uuid='LVM-'+row['vg_uuid'].replace('-','')+row['lv_uuid'].replace('-','')
        with mock.patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,report,'')),mock.patch.object(agent,'_block_dm_uuid',return_value=device_uuid):
            active=agent._lvm_identity(PurePosixPath('/dev/dm-19'),info)
            inactive=agent._lvm_identity(PurePosixPath(row['lv_path']),None)
        self.assertEqual(active,inactive)

    def test_lvm_alias_does_not_match_regular_file_or_wrong_active_uuid(self):
        row=self.lvm_report();report=json.dumps({'report':[{'lv':[row]}]})
        with mock.patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,report,'')),mock.patch.object(agent,'_block_dm_uuid',return_value=''):
            info=types.SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_rdev=0)
            with self.assertRaisesRegex(RuntimeError,'does not point'):agent._lvm_identity(PurePosixPath(row['lv_path']),info)
        with mock.patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,report,'')),mock.patch.object(agent,'_block_dm_uuid',return_value='LVM-'+'z'*64):
            info=types.SimpleNamespace(st_mode=stat.S_IFBLK|0o600,st_rdev=42)
            with self.assertRaisesRegex(RuntimeError,'does not point'):agent._lvm_identity(PurePosixPath(row['lv_path']),info)
            with self.assertRaisesRegex(RuntimeError,'missing from metadata'):agent._lvm_identity(PurePosixPath('/dev/dm-99'),info)

    def test_missing_lvm_device_path_resolves_from_metadata_and_unknown_stays_closed(self):
        row=self.lvm_report();report=json.dumps({'report':[{'lv':[row]}]})
        class MissingPath(PurePosixPath):
            def resolve(self,strict=False):raise FileNotFoundError('inactive volume')
        with mock.patch.object(agent,'Path',side_effect=MissingPath),mock.patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,report,'')):
            self.assertEqual(agent._volume_identity(row['lv_path'])['identity'],['lvm',row['vg_uuid'],row['lv_uuid']])
            with self.assertRaisesRegex(RuntimeError,'Cannot identify disk alias'):agent._volume_identity('/dev/unknown/lv')

    def test_template_and_custom_disk_arguments_require_manual_inspection(self):
        with mock.patch.object(agent,'_exists',return_value=True),mock.patch.object(agent,'_raw_config',return_value=''), \
             mock.patch.object(agent,'vm_config',return_value={'template':'1'}):
            with self.assertRaisesRegex(RuntimeError,'linked-clone'):agent._deletion_check(103)
        (self.root/'vms'/'104.conf').write_text('args: -blockdev driver=file,filename=/hidden/disk\n')
        with self.disk_context('',[],{}):
            with self.assertRaisesRegex(RuntimeError,'custom QEMU disk'):agent._deletion_check(103)

    def test_remove_resume_never_recreates_or_replays_deletion(self):
        for phase in ['REMOVE_REQUESTED','DELETING','DELETED']:
            s=dict(vmid=103,owner='a'*32,operation='remove',phase=phase,snippets={})
            agent._save(s)
            with mock.patch.object(agent,'_lock',return_value=contextlib.nullcontext()),mock.patch.object(agent,'_exists',return_value=False), \
                 mock.patch.object(agent,'_create_vm') as create,mock.patch.object(agent,'_delete') as delete, \
                 mock.patch.object(agent,'_validate_artifacts') as validate:
                self.assertEqual(agent.resume(103)['phase'],'REMOVED')
            create.assert_not_called();delete.assert_not_called();validate.assert_not_called()
        with mock.patch.object(agent,'_exists',return_value=True),mock.patch.object(agent,'_create_vm') as create:
            with self.assertRaisesRegex(RuntimeError,'never deletes or recreates'):agent._continue(dict(s,phase='DELETING'))
        create.assert_not_called()

    def test_remove_records_operation_while_preserving_managed_metadata(self):
        s=state(phase='READY_SOFTWARE',operation='create',important_evidence={'kept':True});agent._save(s)
        with mock.patch.object(agent,'_deletion_check',return_value={'config_sha256':'guard'}), \
             mock.patch.object(agent,'_lock',return_value=contextlib.nullcontext()),mock.patch.object(agent,'_owned'), \
             mock.patch.object(agent,'_delete') as delete,mock.patch.object(agent,'_remove_snippets'):
            value=agent.remove(103,103)
        self.assertEqual(value['operation'],'remove');self.assertEqual(value['important_evidence'],{'kept':True})
        self.assertEqual(delete.call_args.args[0]['phase'],'REMOVED')

    def test_legacy_missing_operation_kind_cannot_recreate_deleted_vm(self):
        s=state(phase='DELETED');s.pop('operation')
        with mock.patch.object(agent,'_create_vm') as create:
            with self.assertRaisesRegex(RuntimeError,'Operation kind'):agent._continue(s)
        create.assert_not_called()

    def test_desktop_wait_is_fresh_normal_user_and_issues_no_browser_input(self):
        replies=[result(returncode=1,stderr='user bus not ready'),result(json.dumps({'status':'DESKTOP_READY','ui_input_issued':False}))]
        with mock.patch.object(agent,'_qga_try',side_effect=replies) as qga,mock.patch.object(agent.time,'sleep'),mock.patch.object(agent,'_save'):
            agent._wait_desktop(state())
        self.assertEqual(qga.call_count,2);source=qga.call_args.args[1][2];compile(source,'<desktop-readiness>','exec')
        for required in ['show-environment','XAUTHORITY','session-key','/proc/uptime','onepve-agent-session.target','onepve-agent-fixture.service']:
            self.assertIn(required,source)
        self.assertNotIn('/mcp',source);self.assertNotIn('browser_',source)

    def test_desktop_timeout_is_bounded_without_input(self):
        with mock.patch.object(agent,'_qga_try',return_value=result(returncode=1,stderr='not ready')), \
             mock.patch.object(agent.time,'monotonic',side_effect=[0,0,181]),mock.patch.object(agent.time,'sleep'):
            with self.assertRaisesRegex(TimeoutError,'no browser input'):agent._wait_desktop(state())

    def test_create_command_chain_meets_resources_and_retains_rescue_display(self):
        s=state(phase='VM_CREATING',artifacts={'image':{'path':'/cache/pinned.qcow2'}},
                resources={'disk_format':'qcow2','snippets_storage':'pool'})
        current={};running=False;calls=[]
        def fake_run(argv,**kwargs):
            nonlocal running
            calls.append(argv)
            if argv[:2]==['qm','create']:
                current.update(description='onePVE owner='+s['owner'],tags='onepve-agent')
            elif argv[:3]==['qm','disk','import']:current['unused0']='pool:103/vm-103-disk-0.qcow2'
            elif argv[:2]==['qm','set']:
                for index in range(3,len(argv),2):current[argv[index][2:]]=argv[index+1]
                if '--scsi0' in argv:current.pop('unused0',None)
            elif argv[:2]==['qm','status']:return subprocess.CompletedProcess(argv,0,'status: '+('running' if running else 'stopped'),'')
            elif argv[:2]==['qm','start']:running=True
            return subprocess.CompletedProcess(argv,0,'','')
        with mock.patch.object(agent,'run',side_effect=fake_run),mock.patch.object(agent,'_exists',side_effect=lambda vmid:bool(current)), \
             mock.patch.object(agent,'vm_config',side_effect=lambda vmid:dict(current)),mock.patch.object(agent,'_save',side_effect=lambda value,phase=None,**changes:(value.update(changes),value.update(phase=phase) if phase else None)), \
             mock.patch.object(agent,'_snippet',return_value='pool:snippets/onepve-'+s['owner']),mock.patch.object(agent,'_wait_cloud'):
            agent._create_vm(s)
        create=calls[0]
        for flag,value in [('--cores','4'),('--memory','8192'),('--balloon','0'),('--bios','ovmf'),('--machine','q35'),('--vga','virtio'),('--scsihw','virtio-scsi-single')]:
            self.assertEqual(create[create.index(flag)+1],value)
        self.assertEqual(s['phase'],'CLOUD_READY');self.assertIn('discard=on',current['scsi0'])
        self.assertIn(['qm','disk','resize','103','scsi0','32G'],calls)
        self.assertEqual(current['ipconfig0'],'ip=dhcp')
        self.assertFalse(any('hostpci0' in str(command) or 'reboot' in command or 'modprobe' in command for command in calls))

    def test_cloud_vm_creation_passes_explicit_static_network_to_pve(self):
        s=state(phase='CLOUD_CONFIGURING',config=config(address='192.168.11.144/24',gateway='192.168.11.1',nameserver='1.1.1.1'),
                resources={'disk_format':'qcow2','snippets_storage':'pool'})
        current={'efidisk0':'pool:efi','ide2':'pool:cloudinit','scsi0':'pool:disk','cicustom':'user=pool:snippets/'+s['owner']}
        def save(value,phase=None,**changes):
            value.update(changes)
            if phase:value['phase']=phase
        with mock.patch.object(agent,'_owned',return_value=current),mock.patch.object(agent,'_snippet',return_value='pool:snippets/'+s['owner']), \
             mock.patch.object(agent,'_save',side_effect=save),mock.patch.object(agent,'_wait_cloud'), \
             mock.patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,'status: running','')) as run:
            agent._create_vm(s)
        command=next(call.args[0] for call in run.call_args_list if '--ipconfig0' in call.args[0])
        self.assertEqual(command[command.index('--ipconfig0')+1],'ip=192.168.11.144/24,gw=192.168.11.1')
        self.assertEqual(command[command.index('--nameserver')+1],'1.1.1.1');self.assertEqual(s['phase'],'CLOUD_READY')


if __name__=='__main__':unittest.main()
