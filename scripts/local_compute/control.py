#!/usr/bin/env python3
"""Persistent on/off control for the existing Mac background research worker."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_launch_agent import launch_document, scheduled_policy

LABEL = 'com.weatheredge.v7.local-research'


def launchctl(*arguments):
    result = subprocess.run(['/bin/launchctl', *arguments], capture_output=True,
                            text=True, timeout=20)
    allowed = (0, 3, 113) if arguments[0] == 'bootout' else (0, 113)
    if result.returncode not in allowed:
        raise RuntimeError('macOS could not read or change the research schedule')
    return result


def worker_running(state):
    with (state / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


class Control:
    def __init__(self, config):
        self.config = Path(config).expanduser().resolve()
        metadata = self.config.stat()
        if metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
            raise ValueError('worker configuration must be private and owned by this user')
        data = json.loads(self.config.read_text())
        scheduled_policy(data)
        self.state = Path(data['state_dir']).expanduser().resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.marker = self.state / 'background-paused'
        self.domain = f'gui/{os.getuid()}'
        self.service = f'{self.domain}/{LABEL}'
        self.plist = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'

    def status(self):
        disabled_result = launchctl('print-disabled', self.domain)
        if disabled_result.returncode:
            raise RuntimeError('macOS research preference is unavailable')
        disabled = bool(re.search(r'"' + re.escape(LABEL) + r'"\s*=>\s*true',
                                  disabled_result.stdout))
        loaded = launchctl('print', self.service).returncode == 0
        running = worker_running(self.state)
        paused = self.marker.exists() or disabled
        enabled = not paused and loaded
        if paused:
            message = 'OFF — stopping current work…' if running else 'OFF — no Mac background research.'
        elif enabled:
            message = 'ON — research is running.' if running else 'ON — waiting for the next scheduled run.'
        else:
            message = 'OFF — research schedule is not loaded.'
        return {'enabled': enabled, 'paused': paused, 'loaded': loaded,
                'running': running, 'message': message}

    def off(self, *, wait_seconds=15):
        staged = self.marker.with_suffix('.tmp')
        staged.write_text(json.dumps({'paused': True, 'changed_at': datetime.now(timezone.utc).isoformat()}) + '\n')
        staged.chmod(0o600)
        staged.replace(self.marker)
        if launchctl('disable', self.service).returncode:
            raise RuntimeError('pause is recorded, but macOS could not disable the schedule')
        deadline = time.monotonic() + wait_seconds
        while worker_running(self.state):
            if time.monotonic() >= deadline:
                raise RuntimeError('Schedule is off; current work is still stopping. Reopen the control to check.')
            time.sleep(.1)
        # Let the budget supervisor clean up its separate child session before
        # unloading the caffeinate wrapper. A timeout never claims work stopped.
        launchctl('bootout', self.service)
        result = self.status()
        if result['loaded'] or result['running'] or not result['paused']:
            raise RuntimeError('Schedule pause could not be verified; reopen the control to check.')
        return result

    def on(self):
        # Only the installed, unchanged, bounded worker can be resumed. Neither
        # an immediate run nor a higher resource/provider budget is requested.
        expected = launch_document(self.config, self.state, sys.executable)
        if plistlib.loads(self.plist.read_bytes()) != expected:
            raise ValueError('installed schedule differs; reinstall it before turning research on')
        current = self.status()
        if current['enabled']:
            return current
        if worker_running(self.state) and self.marker.exists():
            raise RuntimeError('Current work is still stopping; wait before turning research on.')
        try:
            if launchctl('enable', self.service).returncode:
                raise RuntimeError('macOS could not enable the schedule')
            if launchctl('print', self.service).returncode:
                if launchctl('bootstrap', self.domain, str(self.plist)).returncode:
                    raise RuntimeError('macOS could not load the schedule')
            self.marker.unlink(missing_ok=True)
            result = self.status()
            if not result['enabled']:
                raise RuntimeError('Research schedule could not be verified as on.')
            return result
        except Exception:
            self.marker.touch(mode=0o600, exist_ok=True)
            launchctl('disable', self.service)
            launchctl('bootout', self.service)
            raise


def app_source(config):
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), '--config', str(config)])
    literal = json.dumps(command, ensure_ascii=False)
    return f'''on run
    try
        set controlCommand to {literal}
        set currentStatus to do shell script controlCommand & " status --text"
        set choice to button returned of (display dialog currentStatus & return & return & "Runs at most 5 minutes every 2 hours, only on AC power. Unplugging stops the active job at the next power check. Off stays off after restart." with title "WeatherEdge Control" buttons {{"Close", "Turn Off", "Turn On"}} default button "Close")
        if choice is "Turn Off" then
            set outcome to do shell script controlCommand & " off --text"
        else if choice is "Turn On" then
            set outcome to do shell script controlCommand & " on --text"
        else
            return
        end if
        display dialog outcome with title "WeatherEdge Control" buttons {{"Done"}} default button "Done"
    on error errorMessage number errorNumber
        if errorNumber is not -128 then
            display dialog errorMessage with title "WeatherEdge Control" buttons {{"OK"}} default button "OK" with icon caution
        end if
    end try
end run
'''


def install_app(config):
    destination = Path.home() / 'Applications/WeatherEdge Control.app'
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['/usr/bin/osacompile', '-o', str(destination), '-'],
                   input=app_source(config), text=True, check=True, capture_output=True, timeout=30)
    info = destination / 'Contents/Info.plist'
    document = plistlib.loads(info.read_bytes())
    document.update(CFBundleIdentifier='com.weatheredge.local-control',
                    CFBundleName='WeatherEdge Control', LSUIElement=True)
    info.write_bytes(plistlib.dumps(document))
    shortcut = Path.home() / 'Desktop/WeatherEdge Control.app'
    if not shortcut.exists() and not shortcut.is_symlink():
        shortcut.symlink_to(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('action', choices=('status', 'on', 'off', 'install-app'))
    parser.add_argument('--text', action='store_true')
    args = parser.parse_args()
    try:
        control = Control(args.config)
        if args.action == 'install-app':
            print(install_app(control.config))
            return
        with (control.state / 'control.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Research control is already changing; wait a moment and reopen it.')
            value = getattr(control, args.action)()
        print(value['message'] if args.text else json.dumps(value))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
