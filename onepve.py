#!/usr/bin/env python3
"""CLI shared by the PVE menus; importing it never applies configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {'-h', '--help'}:
        print('Usage: onepve.py {gpu|agent|sls} ACTION [options]\n'
              '  gpu   prepare host PCI/SR-IOV resources (never creates a VM)\n'
              '  agent create/verify/rebuild a Debian work VM (never prepares host drivers)\n'
              '  sls   optional host audit export (disabled until budget approval and metadata checks)')
        return 0
    try:
        if argv[0] == 'gpu':
            from onepve_gpu import main as entry
        elif argv[0] == 'agent':
            from onepve_agent import main as entry
        elif argv[0] == 'sls':
            from onepve_sls import main as entry
        else:
            raise ValueError('Choose gpu, agent or sls')
        return entry(argv[1:]) or 0
    except (RuntimeError, ValueError, OSError, TimeoutError, subprocess.SubprocessError) as error:
        menu = os.environ.get('ONEPVE_MENU_ERRORS') == '1'
        print(json.dumps({'status': 'ERROR', 'error': str(error)}, ensure_ascii=False), file=sys.stdout if menu else sys.stderr)
        if menu:
            print('onePVE: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
