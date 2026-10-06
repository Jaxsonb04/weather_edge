#!/usr/bin/env python3
"""Install the authorized, bounded offline-first research schedule on this Mac."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guards import policy

SCHEDULE_INTERVAL_SECONDS = 7200
SCHEDULE_WALL_SECONDS = 300
SCHEDULE_CPU_SECONDS = 120


def scheduled_policy(config):
    """The more frequent schedule requires tighter budgets before any mutation."""
    limits = policy(config)
    if (limits['wall_seconds'] > SCHEDULE_WALL_SECONDS
            or limits['cpu_seconds'] > SCHEDULE_CPU_SECONDS):
        raise ValueError('two-hour schedule requires wall_seconds <= 300 and cpu_seconds <= 120')
    return limits


def launch_document(config, state, python):
    return {'Label': 'com.weatheredge.v7.local-research',
            'ProgramArguments': ['/usr/bin/caffeinate', '-i', str(python),
                                 str(Path(__file__).with_name('worker.py').resolve()),
                                 '--config', str(config), '--scheduled'],
            'StartInterval': SCHEDULE_INTERVAL_SECONDS, 'RunAtLoad': False,
            'ProcessType': 'Background', 'LowPriorityIO': True, 'Nice': 10,
            'ThrottleInterval': 300, 'ExitTimeOut': 10,
            'StandardOutPath': str(state / 'launchd.stdout'),
            'StandardErrorPath': str(state / 'launchd.stderr')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    config = args.config.resolve()
    if config.stat().st_mode & 0o077:
        parser.error('private configuration must have mode 0600')
    data = json.loads(config.read_text())
    try:
        scheduled_policy(data)
    except ValueError as error:
        parser.error(str(error))
    state = Path(data['state_dir']).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    label = 'com.weatheredge.v7.local-research'
    path = Path.home() / 'Library/LaunchAgents' / f'{label}.plist'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(launch_document(config, state, sys.executable)))
    os.chmod(path, 0o600)
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['launchctl', 'enable', f'gui/{os.getuid()}/{label}'], check=True)
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
    print('Installed bounded research schedule: every two hours; no immediate run.')


if __name__ == '__main__':
    main()
