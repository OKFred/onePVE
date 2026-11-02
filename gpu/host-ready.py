#!/usr/bin/env python3
"""Prepare only the selected idle GPU resource before PVE starts guests."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def need(value, message):
    if not value: raise RuntimeError(message)


def driver(device):
    return (device/'driver').resolve().name if (device/'driver').exists() else None


def unused(device):
    group = (device/'iommu_group').resolve(strict=True)
    need([p.name for p in (group/'devices').iterdir()] == [device.name], 'Selected GPU group is not exclusive')
    for conf in Path('/etc/pve/qemu-server').glob('*.conf'):
        for line in conf.read_text().splitlines():
            if line.startswith('['): break
            if re.match(r'^hostpci\d+:', line):
                need('mapping=' not in line, 'Unresolved PCI resource mapping blocks device changes')
                addresses = re.findall(r'(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(?:\.[0-7])?', line)
                referenced = any(x == device.name or x == device.name[5:] or x == device.name[:-2] or x == device.name[5:-2] for x in addresses)
                pid = Path('/run/qemu-server')/(conf.stem+'.pid')
                need(not (referenced and pid.exists() and (Path('/proc')/pid.read_text().strip()).exists()),
                     'A running VM references the GPU; refusing a live rebind')
    targets = {'/dev/vfio/'+group.name} | {'/dev/dri/'+p.name for p in (device/'drm').glob('*')}
    targets |= {'/dev/vfio/devices/'+p.name for p in (device/'vfio-dev').glob('vfio*')}
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal(): continue
        try:
            for fd in (process/'fd').iterdir():
                try: target = os.readlink(fd)
                except (FileNotFoundError, ProcessLookupError): continue
                need(target not in targets and target != 'anon_inode:[vfio-device]', 'A process still holds a GPU/VFIO descriptor')
        except (FileNotFoundError, ProcessLookupError): pass


def bind(device):
    if driver(device) == 'vfio-pci': return
    unused(device)
    subprocess.run(['modprobe','vfio-pci'],check=True)
    (device/'driver_override').write_text('vfio-pci')
    if (device/'driver/unbind').exists(): (device/'driver/unbind').write_text(device.name)
    Path('/sys/bus/pci/drivers_probe').write_text(device.name)
    need(driver(device) == 'vfio-pci', 'Selected device did not bind to vfio-pci')


def start(state):
    device = Path('/sys/bus/pci/devices')/state['bdf']
    need((device/'vendor').read_text().strip()==state['vendor'] and (device/'device').read_text().strip()==state['device_id'], 'PCI hardware identity changed')
    if state['mode']=='passthrough':
        need(not (device/'boot_vga').exists() or (device/'boot_vga').read_text().strip()!='1', 'Cannot rebind the host boot GPU')
        bind(device); return
    if driver(device) is None:
        subprocess.run(['modprobe','i915'],check=True)
    need(driver(device)=='i915', 'SR-IOV PF must remain bound to i915')
    expected=state.get('driver',{}).get('version')
    if expected:
        need(Path('/sys/module/i915/version').read_text().strip()==expected+'-sriov',
             'The pinned SR-IOV module is not running; do not replace a live PF driver')
    enabled=int((device/'sriov_numvfs').read_text());count=state['num_vfs']
    need(enabled in (0,count), 'Existing VF allocation changed; it will not be reset')
    if not enabled:
        unused(device)
        need(int((device/'sriov_totalvfs').read_text())>=count, 'Driver does not expose enough VFs')
        (device/'sriov_drivers_autoprobe').write_text('0')
        (device/'sriov_numvfs').write_text(str(count))
    deadline=time.monotonic()+10
    while len(list(device.glob('virtfn*')))!=count:
        need(time.monotonic()<deadline,'VF creation did not complete');time.sleep(.1)
    children=[p.resolve(strict=True) for p in sorted(device.glob('virtfn*'))]
    for child in children:
        need((child/'physfn').resolve()==device.resolve(),'VF parent changed')
        bind(child)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state',type=Path,required=True);args=parser.parse_args()
    need(os.getuid()==os.geteuid()==0,'Root is required')
    need(args.state.is_absolute() and not args.state.is_symlink(),'Unsafe resource state')
    info=args.state.stat();need(info.st_uid==0 and not info.st_mode&0o077,'Resource state must be root-private')
    state=json.loads(args.state.read_text());need(state.get('owned') is True,'External providers must not be changed')
    need(re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]',state.get('bdf','')),'Invalid PCI identity')
    start(state)
    state['status']='READY';state['last_ready_boot_id']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    temporary=args.state.with_name('.'+args.state.name+'.'+str(os.getpid()))
    with temporary.open('x') as stream:
        json.dump(state,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    temporary.chmod(0o600);temporary.replace(args.state)
    print(json.dumps({'status':'GPU_RESOURCE_READY','bdf':state['bdf']}))


if __name__=='__main__':
    try: main()
    except (OSError,RuntimeError,subprocess.SubprocessError) as error:
        print(json.dumps({'status':'GPU_RESOURCE_NOT_READY','error':str(error)}),file=sys.stderr);sys.exit(1)
