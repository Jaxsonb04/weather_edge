"""Conservative, fail-closed Mac admission and process-group research budgets.

These limits are an operator policy, not a claim that a particular workload or
temperature is medically/hardware safe. Native thermal warnings stop work; no
fan, charging, clock, or power-management setting is changed.
"""
import os
import math
import re
import signal
import subprocess
import sys
import time

THREAD_ENV = {name: '1' for name in (
    'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
    'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS', 'BLIS_NUM_THREADS')}
DEFAULT_LIMITS = {
    'wall_seconds': 1200, 'cpu_seconds': 600, 'cpu_fraction': 0.5,
    'rss_bytes': 2 * 1024**3, 'minimum_free_memory_percent': 25,
    'maximum_load_per_cpu': 0.25, 'minimum_free_disk_bytes': 20 * 1024**3,
}


class Deferred(RuntimeError):
    """A budget or host guard deferred work; successful evidence is preserved."""


class GroupCleanupError(RuntimeError):
    """Owned group cleanup failed; never a benign defer or collector exit."""
    def __init__(self, cleanup_error, primary_error=None):
        self.cleanup_error = cleanup_error
        self.primary_error = primary_error
        details = type(cleanup_error).__name__
        if getattr(cleanup_error, 'errno', None) is not None:
            details += ' errno=' + str(cleanup_error.errno)
        if primary_error is not None:
            details += '; original=' + type(primary_error).__name__
        super().__init__('process-group cleanup failed: ' + details)


def policy(config):
    """Only tighter limits may be selected without changing reviewed source."""
    result = dict(DEFAULT_LIMITS)
    for key, value in config.get('limits', {}).items():
        if (key not in result or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ValueError('invalid local compute limit')
        lower_is_safer = key not in ('minimum_free_memory_percent', 'minimum_free_disk_bytes')
        if value <= 0 or (value > result[key] if lower_is_safer else value < result[key]):
            raise ValueError('local compute limits may only be tightened')
        result[key] = value
    return result


def _output(command):
    return subprocess.check_output(command, text=True, timeout=5, stderr=subprocess.DEVNULL)


def thermal_ok(output):
    # Both the no-warning form and explicit normal values occur on macOS.
    for line in output.splitlines():
        if line.startswith('Note: No ') and 'recorded' in line:
            continue
        if '=' in line:
            key, value = [part.strip() for part in line.split('=', 1)]
            if key in ('CPU_Speed_Limit', 'CPU_Scheduler_Limit') and value != '100':
                return False
            if key in ('Thermal_Level', 'Performance_Level') and value != '0':
                return False
    return bool(output.strip()) and ('No thermal warning' in output or
                                    'Thermal_Level' in output or
                                    'CPU_Speed_Limit' in output)


def host_snapshot(state, limits):
    if sys.platform != 'darwin':
        raise Deferred('local schedule requires native macOS power and thermal checks')
    try:
        battery = _output(['/usr/bin/pmset', '-g', 'batt'])
        thermal = _output(['/usr/bin/pmset', '-g', 'therm'])
        custom = _output(['/usr/bin/pmset', '-g', 'custom'])
        pressure = _output(['/usr/bin/memory_pressure', '-Q'])
        cpus = int(_output(['/usr/sbin/sysctl', '-n', 'hw.logicalcpu']).strip())
        free = re.search(r'System-wide memory free percentage:\s*(\d+)%', pressure)
        ac = custom.split('AC Power:', 1)[1]
        low = re.search(r'^\s*(?:lowpowermode|powermode)\s+(\d+)', ac, re.M)
        if 'AC Power' not in battery.splitlines()[0]:
            raise Deferred('AC power is required')
        if low is None or int(low.group(1)) == 1:
            raise Deferred('low-power mode or unavailable power policy')
        if not thermal_ok(thermal):
            raise Deferred('native thermal or performance warning')
        if free is None or int(free.group(1)) < limits['minimum_free_memory_percent']:
            raise Deferred('insufficient system memory headroom')
        load = os.getloadavg()[0]
        if load > cpus * limits['maximum_load_per_cpu']:
            raise Deferred('other computer work is using the load budget')
        disk = os.statvfs(state)
        free_disk = disk.f_bavail * disk.f_frsize
        if free_disk < limits['minimum_free_disk_bytes']:
            raise Deferred('insufficient local disk headroom')
        return {'ac_power': True, 'native_thermal_warning': False,
                'free_memory_percent': int(free.group(1)), 'load_1m': round(load, 3),
                'logical_cpus': cpus, 'free_disk_bytes': free_disk}
    except Deferred:
        raise
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        raise Deferred('native resource checks unavailable') from error


def cpu_clock(value):
    """Parse macOS ps TIME (MM:SS.cc, HH:MM:SS, optional day prefix)."""
    days = 0
    if '-' in value:
        day, value = value.split('-', 1)
        days = int(day)
    total = 0.0
    for part in value.split(':'):
        total = total * 60 + float(part)
    return days * 86400 + total


def process_group_sample(group):
    rows = []
    for line in _output(['/bin/ps', '-axo', 'pid=,pgid=,rss=,time=']).splitlines():
        pid, pgid, rss, clock = line.split()
        if int(pgid) == group:
            rows.append((int(pid), int(rss) * 1024, cpu_clock(clock)))
    return rows


def signal_group(process, number):
    """Recover Darwin's exited-unreaped race only after reaping our child."""
    try:
        os.killpg(process.pid, number)
    except (ProcessLookupError, PermissionError):
        if process.poll() is None:
            raise  # A live child never receives a blanket permission exemption.
        if number != signal.SIGKILL:
            return False
        # Reaping the group leader can remove a zombie-only group. Still retry
        # cleanup: live descendants must be killed even after the leader exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


class Budget:
    def __init__(self, state, limits):
        self.state = state
        self.limits = limits
        self.started = time.monotonic()
        self.cpu_seconds = 0.0
        self.maximum_rss_bytes = 0

    def run(self, command, *, stdout, stderr=None, stdin=None, timeout=None):
        remaining = self.limits['wall_seconds'] - (time.monotonic() - self.started)
        if remaining <= 0:
            raise Deferred('whole-run wall-time budget exhausted')
        child_limit = min(remaining, timeout or remaining)
        host_snapshot(self.state, self.limits)
        environment = {**os.environ, **THREAD_ENV, 'WEATHEREDGE_LOCAL_BUDGET_ACTIVE': '1'}
        started = time.monotonic()
        seen = {}
        paused = False
        checked = started
        process = None
        previous_handlers = {}
        cancelled = False
        def cancel(signum, frame):
            nonlocal cancelled
            cancelled = True
            # If Popen has forked but has not returned its object, record the
            # signal and clean up as soon as we own that process handle.
            if process is not None:
                raise Deferred('local computation cancelled by supervisor termination')
        try:
            # Install before creating the separate child session, so bootout,
            # terminal loss or graceful termination enters group cleanup.
            for number in (signal.SIGTERM, signal.SIGHUP):
                previous_handlers[number] = signal.signal(number, cancel)
            if cancelled:
                raise Deferred('local computation cancelled by supervisor termination')
            process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr,
                                       env=environment, start_new_session=True)
            if cancelled:
                raise Deferred('local computation cancelled by supervisor termination')
            while process.poll() is None:
                now = time.monotonic()
                if now - started > child_limit:
                    raise Deferred('job wall-time budget exhausted')
                rows = process_group_sample(process.pid)
                for pid, _, cpu in rows:
                    seen[pid] = max(seen.get(pid, 0), cpu)
                cpu = sum(seen.values())
                rss = sum(row[1] for row in rows)
                self.maximum_rss_bytes = max(self.maximum_rss_bytes, rss)
                if self.cpu_seconds + cpu >= self.limits['cpu_seconds']:
                    raise Deferred('whole-run CPU budget exhausted')
                if rss > self.limits['rss_bytes']:
                    raise Deferred('process-group memory budget exhausted')
                if now - checked >= 10:
                    host_snapshot(self.state, self.limits)
                    checked = now
                # Small bursts are allowed; aggregate CPU is paced to half one
                # logical core, including descendants even if libraries spawn.
                should_pause = cpu > (now - started) * self.limits['cpu_fraction'] + 0.25
                if should_pause != paused:
                    if not signal_group(process, signal.SIGSTOP if should_pause else signal.SIGCONT):
                        break
                    paused = should_pause
                time.sleep(0.25)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
        finally:
            self.cpu_seconds += sum(seen.values())
            primary_error = sys.exc_info()[1]
            # Kill the entire group on timeout, warning, cancellation or a
            # surviving descendant; never leave a paused worker behind.
            try:
                if process is not None:
                    cleanup_error = None
                    try:
                        signal_group(process, signal.SIGKILL)
                    except BaseException as error:
                        cleanup_error = error
                    try:
                        process.wait(timeout=5)
                    except BaseException as error:
                        if cleanup_error is None:
                            cleanup_error = error
                        else:
                            cleanup_error.add_note('child reaping also failed: ' + type(error).__name__)
                    if cleanup_error is not None:
                        # Notes alone would retain a benign exit/defer type and
                        # let callers continue while group cleanup was unproved.
                        # This distinct fatal error keeps the original cause.
                        raise GroupCleanupError(cleanup_error, primary_error) from (primary_error or cleanup_error)
            finally:
                for number, previous in previous_handlers.items():
                    signal.signal(number, previous)

    def receipt(self):
        return {'elapsed_seconds': round(time.monotonic() - self.started, 3),
                'observed_cpu_seconds': round(self.cpu_seconds, 3),
                'maximum_group_rss_bytes': self.maximum_rss_bytes,
                'limits': self.limits, 'thread_environment': THREAD_ENV}
