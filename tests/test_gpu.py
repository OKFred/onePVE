import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_gpu as gpu


def device(bdf='0000:00:02.0', **changes):
    value = dict(bdf=bdf, vendor='0x8086', device='0x7d67', driver='i915',
                 iommu_group='1', group_members=[bdf], physfn=None, vfs=[],
                 enabled_vfs=0, total_vfs=1, boot_vga=False, consumers=[])
    value.update(changes)
    return value


class GpuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = __import__('contextlib').ExitStack()
        self.stack.enter_context(mock.patch.object(gpu, 'require_root'))
        self.stack.enter_context(mock.patch.object(gpu, 'GPU_STATE', self.root/'state'))
        self.stack.enter_context(mock.patch.object(gpu.os, 'uname', return_value=types.SimpleNamespace(release='7.0.14-17-pve', machine='x86_64'), create=True))

    def tearDown(self):
        self.stack.close(); self.temp.cleanup()

    def test_kernel_pins_and_unsupported(self):
        self.assertEqual(gpu.driver_spec('6.12.113+deb13-amd64')['version'], '2025.12.10.2')
        self.assertEqual(gpu.driver_spec('6.18.1-pve')['version'], '2026.10.28')
        self.assertEqual(gpu.driver_spec('7.3.1-pve')['version'], '2026.10.28')
        for kernel in ['6.8.12-pve', '7.4.0', 'unknown']:
            with self.assertRaises(RuntimeError): gpu.driver_spec(kernel)

    def test_bdf_is_not_a_shell_or_mapping(self):
        self.assertEqual(gpu.canonical_bdf('00:02.1'), '0000:00:02.1')
        for value in ['00:02', '0000:00:02.8', '../00:02.1', '00:02.1;reboot', 'mapping=foo']:
            with self.assertRaises(RuntimeError): gpu.canonical_bdf(value)

    def test_vm_refs_stop_before_snapshot_history(self):
        (self.root/'103.conf').write_text('hostpci0: host=00:02.1,pcie=1\n[snapshot]\nhostpci1: 00:03.0\n')
        (self.root/'104.conf').write_text('hostpci0: mapping=external\n')
        with mock.patch.object(gpu,'VM_CONFIGS',self.root): refs, unknown=gpu._pci_refs()
        self.assertEqual(refs,[{'kind':'vm_config','vmid':'103','bdf':'0000:00:02.1'}])
        self.assertEqual(unknown,[{'kind':'unresolved_mapping','vmid':'104'}])

    def test_all_function_reference_matches_vf(self):
        self.assertTrue(gpu._matches('0000:00:02','0000:00:02.1'))
        self.assertFalse(gpu._matches('0000:00:02.0','0000:00:02.1'))

    def test_sriov_dry_run_has_zero_writes_or_commands(self):
        with mock.patch.object(gpu,'_one',return_value=device()), mock.patch.object(gpu,'_save') as save, \
             mock.patch.object(gpu,'run') as run, mock.patch.object(gpu,'_ensure_driver') as build:
            result=gpu.prepare_sriov('0000:00:02.0',dry_run=True)
        self.assertEqual(result['status'],'DRY_RUN');save.assert_not_called();run.assert_not_called();build.assert_not_called()
        self.assertFalse((self.root/'state').exists())

    def test_existing_busy_vf_adopts_without_rebind_or_driver_install(self):
        pf=device(vfs=['0000:00:02.1'],enabled_vfs=1)
        vf=device('0000:00:02.1',driver='vfio-pci',physfn=pf['bdf'],consumers=[{'vmid':'103','kind':'vm_config'}])
        with mock.patch.object(gpu,'_one',side_effect=lambda b:pf if b==pf['bdf'] else vf), \
             mock.patch.object(gpu,'_save') as save, mock.patch.object(gpu,'run') as run, \
             mock.patch.object(gpu,'_ensure_driver') as build:
            result=gpu._prepare_sriov(pf['bdf'])
        self.assertFalse(result['owned']);self.assertEqual(result['status'],'EXTERNAL_READY')
        run.assert_not_called();build.assert_not_called();save.assert_called_once()

    def test_external_adoption_dry_run_does_not_write_state(self):
        pf=device(vfs=['0000:00:02.1'],enabled_vfs=1)
        vf=device('0000:00:02.1',driver='vfio-pci',physfn=pf['bdf'])
        with mock.patch.object(gpu,'_one',side_effect=[pf,vf]), mock.patch.object(gpu,'_save') as save:
            result=gpu.prepare_sriov(pf['bdf'],dry_run=True)
        save.assert_not_called();self.assertTrue(result['dry_run'])

    def test_repeated_owned_resource_does_not_become_external(self):
        pf=device(enabled_vfs=1,vfs=['0000:00:02.1'])
        (self.root/'state').mkdir()
        state=dict(resource_id='0000_00_02_0',bdf=pf['bdf'],mode='sriov',status='NEEDS_REBOOT',owned=True,files={})
        (self.root/'state/0000_00_02_0.json').write_text(json.dumps(state))
        with mock.patch.object(gpu,'_save') as save:
            result=gpu._adopt(pf,'sriov',False)
        self.assertTrue(result['owned']);self.assertEqual(result['status'],'READY');save.assert_called_once()

    def test_existing_vfs_are_never_reset_or_expanded(self):
        with mock.patch.object(gpu,'_one',return_value=device(enabled_vfs=1)), mock.patch.object(gpu,'_save') as save:
            with self.assertRaisesRegex(RuntimeError,'not be reset or expanded'): gpu.prepare_sriov('0000:00:02.0',2,True)
        save.assert_not_called()

    def test_occupied_pf_fails_before_preparation(self):
        with mock.patch.object(gpu,'_one',return_value=device(consumers=[{'vmid':'102'}])), mock.patch.object(gpu,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'occupied'): gpu.prepare_sriov('0000:00:02.0',dry_run=True)
        run.assert_not_called()

    def test_target_vmid_can_reuse_own_reference(self):
        vf=device('0000:00:02.1',driver='vfio-pci',physfn='0000:00:02.0',consumers=[{'vmid':'103','kind':'device_fd'}])
        with mock.patch.object(gpu,'_one',side_effect=[vf,device()]): self.assertEqual(gpu.validate_assignment(vf['bdf'],103),vf)
        with mock.patch.object(gpu,'_one',side_effect=[vf,device()]):
            with self.assertRaisesRegex(RuntimeError,'occupied'): gpu.validate_assignment(vf['bdf'],104)

    def test_unmapped_fd_is_not_allowed_even_for_target(self):
        vf=device('0000:00:02.1',driver='vfio-pci',physfn='0000:00:02.0',consumers=[{'vmid':None,'kind':'device_fd'}])
        with mock.patch.object(gpu,'_one',side_effect=[vf,device()]):
            with self.assertRaisesRegex(RuntimeError,'occupied'): gpu.validate_assignment(vf['bdf'],103)

    def test_shared_group_and_boot_gpu_refused(self):
        for item in [device(group_members=['0000:00:02.0','0000:00:02.1']),device(boot_vga=True)]:
            with mock.patch.object(gpu,'_one',return_value=item):
                with self.assertRaises(RuntimeError): gpu.prepare_passthrough(item['bdf'],True)

    def test_restore_external_never_touches_provider(self):
        (self.root/'state').mkdir()
        (self.root/'state/0000_00_02_0.json').write_text(json.dumps(dict(resource_id='0000_00_02_0',bdf='0000:00:02.0',owned=False)))
        with mock.patch.object(gpu,'_one',return_value=device()),mock.patch.object(gpu,'_save') as save,mock.patch.object(gpu,'run') as run:
            result=gpu._restore('0000_00_02_0')
        self.assertEqual(result['status'],'EXTERNAL_PRESERVED');run.assert_not_called();save.assert_called_once()

    def test_restore_refuses_foreign_file_edits(self):
        (self.root/'state').mkdir()
        state=dict(resource_id='0000_00_02_0',bdf='0000:00:02.0',owned=True,status='FAILED',mode='sriov',
                   files={'/managed':{'before':{'absent':True},'after':{'sha256':'owned'}}})
        (self.root/'state/0000_00_02_0.json').write_text(json.dumps(state))
        with mock.patch.object(gpu,'_one',return_value=device()),mock.patch.object(gpu,'_fingerprint',return_value={'sha256':'foreign'}),mock.patch.object(gpu,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'externally modified'):gpu._restore('0000_00_02_0',True)
        run.assert_not_called()

    def test_interrupted_write_requires_exact_intended_bytes_and_owner(self):
        entry={'before':{'absent':True},'intent_sha256':'expected','intent_mode':0o644}
        good={'sha256':'expected','uid':0,'gid':0,'mode':0o644}
        self.assertTrue(gpu._file_owned(entry,good))
        for change in [{'sha256':'foreign'},{'uid':1000},{'mode':0o666}]: self.assertFalse(gpu._file_owned(entry,{**good,**change}))

    def test_unsafe_archive_members_rejected(self):
        archive=self.root/'source.tar.gz'
        with tarfile.open(archive,'w:gz') as stream:
            item=tarfile.TarInfo('../escape');item.size=0;stream.addfile(item)
        with self.assertRaisesRegex(RuntimeError,'archive paths'):gpu._extract_source(archive,self.root/'out')
        self.assertFalse((self.root/'out').exists())

    def test_vermagic_and_companion_symbol_crcs(self):
        modules=[]
        for name in ['i915','xe','kvmgt','intel_sriov_compat']:
            path=self.root/(name+'.ko');path.write_bytes(b'ELFsynthetic');modules.append(path)
        def command(argv,**kwargs):
            value='7.0.14-17-pve SMP' if argv[0]=='modinfo' else '' if '--show-exports' in argv else '0x12345678 module_layout\n'
            return subprocess.CompletedProcess(argv,0,value,'')
        with mock.patch.object(gpu,'run',side_effect=command),mock.patch.object(Path,'is_file',return_value=True), \
             mock.patch.object(Path,'read_text',return_value='0x12345678 module_layout vmlinux EXPORT_SYMBOL\n'),mock.patch.object(gpu,'_sha',return_value='digest'):
            self.assertEqual(len(gpu._verify_built_modules('7.0.14-17-pve',self.root,modules)),4)
        with mock.patch.object(gpu,'run',side_effect=command),mock.patch.object(Path,'is_file',return_value=True), \
             mock.patch.object(Path,'read_text',return_value='0x87654321 module_layout vmlinux EXPORT_SYMBOL\n'),mock.patch.object(gpu,'_sha',return_value='digest'):
            with self.assertRaisesRegex(RuntimeError,'CRC mismatch'):gpu._verify_built_modules('7.0.14-17-pve',self.root,modules)

    def test_crc_checks_use_actual_companion_exports_after_dkms_cleans_build(self):
        modules=[self.root/(name+'.ko.xz') for name in ['i915','xe','kvmgt','intel_sriov_compat']]
        kernel='0x11111111 module_layout vmlinux EXPORT_SYMBOL\n0xdeadbeef companion_symbol drivers/gpu/drm/i915/i915 EXPORT_SYMBOL\n'
        def command(argv,**kwargs):
            if argv[0]=='modinfo':return subprocess.CompletedProcess(argv,0,'6.12.113+deb13-amd64 SMP modversions','')
            name=Path(argv[-1]).name.split('.ko')[0]
            if '--show-exports' in argv:
                if name=='i915':return subprocess.CompletedProcess(argv,0,'0x22222222 companion_symbol\n','')
                return subprocess.CompletedProcess(argv,1,'','modprobe: FATAL: could not get symbols: No data available\n')
            imports='0x11111111 module_layout\n'+('0x22222222 companion_symbol\n' if name!='i915' else '')
            return subprocess.CompletedProcess(argv,0,imports,'')
        with mock.patch.object(gpu,'run',side_effect=command),mock.patch.object(Path,'is_file',return_value=True) as exists, \
             mock.patch.object(Path,'read_text',return_value=kernel),mock.patch.object(gpu,'_sha',return_value='digest'):
            proof=gpu._verify_built_modules('6.12.113+deb13-amd64',self.root/'missing-build',modules)
        self.assertEqual([x['symbols_checked'] for x in proof],[1,2,2,2]);self.assertEqual(sum(x['companion_symbols_checked'] for x in proof),3)
        self.assertEqual(exists.call_count,1)

    def test_absent_real_export_cannot_resolve_from_stock_companion_table(self):
        modules=[self.root/(name+'.ko') for name in ['i915','xe','kvmgt','intel_sriov_compat']]
        kernel='0x11111111 module_layout vmlinux EXPORT_SYMBOL\n0xdeadbeef removed_export drivers/gpu/drm/i915/i915 EXPORT_SYMBOL\n'
        def command(argv,**kwargs):
            value='6.12.1 SMP' if argv[0]=='modinfo' else '' if '--show-exports' in argv else '0xdeadbeef removed_export\n'
            return subprocess.CompletedProcess(argv,0,value,'')
        with mock.patch.object(gpu,'run',side_effect=command),mock.patch.object(Path,'is_file',return_value=True), \
             mock.patch.object(Path,'read_text',return_value=kernel),mock.patch.object(gpu,'_sha',return_value='digest'):
            with self.assertRaisesRegex(RuntimeError,'CRC mismatch: removed_export'):gpu._verify_built_modules('6.12.1',self.root,modules)

    def test_export_error_and_malformed_crc_are_not_silently_ignored(self):
        modules=[self.root/(name+'.ko') for name in ['i915','xe','kvmgt','intel_sriov_compat']]
        for export in [subprocess.CompletedProcess([],1,'','Permission denied'),subprocess.CompletedProcess([],0,'not-a-crc symbol\n','')]:
            def command(argv,**kwargs):
                if argv[0]=='modinfo':return subprocess.CompletedProcess(argv,0,'6.12.1 SMP','')
                return export
            with mock.patch.object(gpu,'run',side_effect=command),mock.patch.object(Path,'is_file',return_value=True), \
                 mock.patch.object(Path,'read_text',return_value='0x11111111 module_layout vmlinux EXPORT_SYMBOL\n'),mock.patch.object(gpu,'_sha',return_value='digest'):
                with self.assertRaisesRegex(RuntimeError,'Cannot inspect|Invalid symbol CRC'):gpu._verify_built_modules('6.12.1',self.root,modules)

    def test_all_four_modules_and_each_import_table_are_required(self):
        modules=[self.root/(name+'.ko') for name in ['i915','xe','kvmgt','intel_sriov_compat']]
        with self.assertRaisesRegex(RuntimeError,'four companion'):gpu._verify_built_modules('6.12.1',self.root,modules[:3])
        def command(argv,**kwargs):return subprocess.CompletedProcess(argv,0,'6.12.1 SMP' if argv[0]=='modinfo' else '','')
        with mock.patch.object(gpu,'run',side_effect=command),mock.patch.object(Path,'is_file',return_value=True), \
             mock.patch.object(Path,'read_text',return_value='0x11111111 module_layout vmlinux EXPORT_SYMBOL\n'),mock.patch.object(gpu,'_sha',return_value='digest'):
            with self.assertRaisesRegex(RuntimeError,'no checked symbol'):gpu._verify_built_modules('6.12.1',self.root,modules)

    def test_boot_fragment_preserves_unrelated_blacklists(self):
        with mock.patch.object(gpu,'_read',return_value='quiet intel_iommu=on module_blacklist=nouveau'), \
             mock.patch.object(gpu,'boot_method',return_value='grub'), mock.patch.object(gpu,'_owned_file') as write, mock.patch.object(gpu,'run'):
            gpu._stage_boot({'resource_id':'0000_00_02_0'},['intel_iommu=on','module_blacklist=xe'])
        self.assertIn('module_blacklist=nouveau,xe',write.call_args.args[2])

    def test_conflicting_boot_parameters_fail_before_writes(self):
        with mock.patch.object(gpu,'_read',return_value='i915.max_vfs=7'),mock.patch.object(gpu,'_owned_file') as write:
            with self.assertRaisesRegex(RuntimeError,'Conflicting boot parameter'):gpu._stage_boot({},['i915.max_vfs=1'])
        write.assert_not_called()

    def test_service_does_not_gate_all_guests_on_gpu_success(self):
        helper=self.root/'gpu/host-ready.py';helper.parent.mkdir();helper.write_text('# helper')
        state={'resource_id':'0000_00_02_0','bdf':'0000:00:02.0'}
        with mock.patch.object(gpu,'repo_root',return_value=self.root),mock.patch.object(gpu,'_owned_file') as write, \
             mock.patch.object(gpu,'_save'),mock.patch.object(gpu,'run'):
            gpu._install_service(state)
        service=write.call_args.args[2]
        self.assertIn('Before=pve-guests.service',service)
        self.assertNotIn('Requires=pve-guests.service',service)
        self.assertNotIn('RequiredBy=',service)
        self.assertNotIn('ExecStop=',service)


if __name__=='__main__':unittest.main()
