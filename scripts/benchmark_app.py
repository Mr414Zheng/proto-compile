#!/usr/bin/env python3
"""测量真实可见的 macOS 主界面及其全部子进程。

应用须支持 --performance-test INPUT OUTPUT_PARENT RUNTIME，并输出
benchmark_version=1 的阶段起止 NDJSON。基线与候选使用完全相同的界面测试驱动，
不以无窗口的 --integration-test 代替真实界面场景。
"""

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
PHASES = ('launch', 'idle', 'inspect_input', 'inspect_output', 'convert', 'after', 'shutdown')
MIB = 1024 * 1024
_MACH_CLOCK = None
_MACH_NUMER = 1
_MACH_DENOM = 1


def monotonic_ns():
    # macOS Python 3.9 的 time.monotonic 使用进程私有原点；Swift DispatchTime
    # 使用系统启动原点，因此显式通过 mach_absolute_time 统一两个进程的时钟。
    return _MACH_CLOCK() * _MACH_NUMER // _MACH_DENOM


class RUsageV2(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64) for name in (
            'user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups',
            'pageins', 'wired_size', 'resident_size', 'phys_footprint',
            'proc_start_abstime', 'proc_exit_abstime', 'child_user_time',
            'child_system_time', 'child_pkg_idle_wkups', 'child_interrupt_wkups',
            'child_pageins', 'child_elapsed_abstime', 'diskio_bytesread',
            'diskio_byteswritten')]


class RUsageV4(ctypes.Structure):
    _fields_ = RUsageV2._fields_ + [(name, ctypes.c_uint64) for name in (
        'cpu_time_qos_default', 'cpu_time_qos_maintenance', 'cpu_time_qos_background',
        'cpu_time_qos_utility', 'cpu_time_qos_legacy', 'cpu_time_qos_user_initiated',
        'cpu_time_qos_user_interactive', 'billed_system_time', 'serviced_system_time',
        'logical_writes', 'lifetime_max_phys_footprint', 'instructions', 'cycles',
        'billed_energy', 'serviced_energy', 'interval_max_phys_footprint', 'runnable_time')]


class TaskInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'virtual_size', 'resident_size', 'total_user', 'total_system',
        'threads_user', 'threads_system')] + [(name, ctypes.c_int32) for name in (
            'policy', 'faults', 'pageins', 'cow_faults', 'messages_sent',
            'messages_received', 'syscalls_mach', 'syscalls_unix', 'csw',
            'threadnum', 'numrunning', 'priority')]


class Timebase(ctypes.Structure):
    _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]


class ProcessAPI:
    def __init__(self):
        global _MACH_CLOCK, _MACH_NUMER, _MACH_DENOM
        if platform.system() != 'Darwin':
            raise RuntimeError('This benchmark requires macOS libproc and /usr/bin/time.')
        self.library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        self.library.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        self.library.proc_pid_rusage.restype = ctypes.c_int
        self.library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                             ctypes.c_void_p, ctypes.c_int]
        self.library.proc_pidinfo.restype = ctypes.c_int
        self.library.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.library.proc_listchildpids.restype = ctypes.c_int
        self.library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.library.proc_pidpath.restype = ctypes.c_int
        base = Timebase()
        system = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        system.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
        if system.mach_timebase_info(ctypes.byref(base)) != 0 or not base.denom:
            raise RuntimeError('Cannot read mach_timebase_info.')
        self.timebase = {'numer': base.numer, 'denom': base.denom}
        system.mach_absolute_time.argtypes = []
        system.mach_absolute_time.restype = ctypes.c_uint64
        _MACH_CLOCK = system.mach_absolute_time
        _MACH_NUMER, _MACH_DENOM = base.numer, base.denom
        # ri_*_time 是 Mach 时钟刻度，不是纳秒；本机 arm64 的换算因子为 125/3。
        self.tick_seconds = base.numer / base.denom / 1_000_000_000

    def children(self, pid):
        capacity = 256
        while capacity <= 16384:
            values = (ctypes.c_int * capacity)()
            ctypes.set_errno(0)
            count = self.library.proc_listchildpids(pid, values, ctypes.sizeof(values))
            error = ctypes.get_errno()
            if count < 0 or (count == 0 and error in (errno.EPERM, errno.EACCES)):
                raise OSError(error, f'proc_listchildpids({pid}): {os.strerror(error)}')
            # 此接口返回 PID 数量；不能按返回字节数的 proc_listpids 接口解读。
            if count < capacity:
                return [value for value in values[:max(0, count)] if value > 0]
            capacity *= 2
        raise RuntimeError('Child PID buffer exhausted; refusing incomplete process-tree data.')

    def read(self, pid):
        usage = RUsageV4()
        ctypes.set_errno(0)
        if self.library.proc_pid_rusage(pid, 4, ctypes.byref(usage)) != 0:
            error = ctypes.get_errno()
            if error in (errno.ESRCH, errno.ENOENT):
                return None
            raise OSError(error, f'proc_pid_rusage({pid}): {os.strerror(error)}')
        task = TaskInfo()
        size = self.library.proc_pidinfo(pid, 4, 0, ctypes.byref(task), ctypes.sizeof(task))
        # 僵尸进程可能仍有 rusage，但已无法查询 task info。
        rss = task.resident_size if size == ctypes.sizeof(task) else usage.resident_size
        return {
            'pid': pid, 'start_ticks': usage.proc_start_abstime,
            'exited': bool(usage.proc_exit_abstime),
            'cpu_seconds': (usage.user_time + usage.system_time) * self.tick_seconds,
            'rss_bytes': rss, 'footprint_bytes': usage.phys_footprint,
            'lifetime_peak_footprint_bytes': usage.lifetime_max_phys_footprint,
            'reaped_child_cpu_seconds':
                (usage.child_user_time + usage.child_system_time) * self.tick_seconds,
        }

    def path(self, pid):
        buffer = ctypes.create_string_buffer(4096)
        count = self.library.proc_pidpath(pid, buffer, len(buffer))
        return os.fsdecode(buffer.value) if count > 0 else '<exited before path capture>'


class TreeSampler:
    def __init__(self, api, wrapper_pid):
        self.api = api
        self.wrapper_pid = wrapper_pid
        self.known = {wrapper_pid}
        self.records = {}
        self.samples = []

    def sample(self):
        queue = list(self.known)
        discovered = set(queue)
        while queue:
            parent = queue.pop()
            for child in self.api.children(parent):
                if child not in discovered:
                    discovered.add(child)
                    queue.append(child)
        current = []
        alive = {self.wrapper_pid}
        for pid in discovered - {self.wrapper_pid}:
            value = self.api.read(pid)
            if value is None:
                continue
            alive.add(pid)
            key = f'{pid}:{value["start_ticks"]}'
            if key not in self.records:
                self.records[key] = dict(value, path=self.api.path(pid), samples=0,
                                         first_seen_ns=monotonic_ns())
            record = self.records[key]
            current_path = self.api.path(pid)
            if not current_path.startswith('<exited'):
                record['path'] = current_path
            record['peak_rss_bytes'] = max(record.get('peak_rss_bytes', 0), value['rss_bytes'])
            record['peak_footprint_bytes'] = max(record.get('peak_footprint_bytes', 0), value['footprint_bytes'])
            value['lifetime_peak_footprint_bytes'] = max(record.get('lifetime_peak_footprint_bytes', 0),
                                                        value['lifetime_peak_footprint_bytes'])
            record.update(value)
            record['samples'] += 1
            record['last_seen_ns'] = monotonic_ns()
            if not value['exited']:
                current.append(value)
        self.known = alive
        sample = {
            'monotonic_ns': monotonic_ns(),
            'sampled_tree_cpu_seconds': sum(item['cpu_seconds'] for item in self.records.values()),
            'aggregate_rss_bytes': sum(item['rss_bytes'] for item in current),
            'aggregate_footprint_bytes': sum(item['footprint_bytes'] for item in current),
            'observed_concurrency_lifetime_footprint_bytes':
                sum(item['lifetime_peak_footprint_bytes'] for item in current),
            'live_process_count': len(current), 'pids': [item['pid'] for item in current],
            'process_values': current,
        }
        self.samples.append(sample)
        return sample


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def input_fingerprint(path):
    """固定输入内容证据；目录同时记录相对路径、逐文件摘要和汇总摘要。"""
    if path.is_file():
        content = path.read_bytes()
        return {'kind': 'file', 'sha256': hashlib.sha256(content).hexdigest(), 'size_bytes': len(content)}
    if not path.is_dir():
        raise RuntimeError(f'Benchmark input does not exist: {path}')
    files = []
    for source in sorted(path.rglob('*.proto')):
        if source.is_file():
            content = source.read_bytes()
            files.append({'path': source.relative_to(path).as_posix(),
                          'sha256': hashlib.sha256(content).hexdigest(), 'size_bytes': len(content)})
    if not files:
        raise RuntimeError(f'Benchmark input directory contains no .proto files: {path}')
    serialized = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'kind': 'proto_directory', 'proto_count': len(files), 'files': files,
            'sha256': hashlib.sha256(serialized).hexdigest()}


def output_fingerprint(path):
    """测量结束后核对实际成果，避免少做工作却被当作性能提升。"""
    if {entry.name for entry in path.iterdir()} != {'java', 'proto'}:
        raise RuntimeError('Converted result must contain only java and proto directories.')
    files = [{'path': source.relative_to(path).as_posix(),
              'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
             for source in sorted(path.rglob('*')) if source.is_file()]
    serialized = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'sha256': hashlib.sha256(serialized).hexdigest(), 'files': files,
            'proto_count': len(list((path / 'proto').rglob('*.proto'))),
            'java_count': len(list((path / 'java').rglob('*.java')))}


def parse_system_time(text):
    parsed = {}
    for field in ('real', 'user', 'sys'):
        match = re.search(r'^' + field + r'\s+(\d+(?:\.\d+)?)\s*$', text, re.MULTILINE)
        if not match:
            raise RuntimeError(f'/usr/bin/time did not report {field}; run outside the sandbox.\n{text}')
        parsed[field + '_seconds'] = float(match[1])
    parsed['tree_cpu_seconds'] = parsed['user_seconds'] + parsed['sys_seconds']
    for label, key in [('maximum resident set size', 'max_single_process_rss_bytes'),
                       ('peak memory footprint', 'max_single_process_footprint_bytes')]:
        match = re.search(r'^\s*(\d+)\s+' + label + r'\s*$', text, re.MULTILINE)
        if match:
            parsed[key] = int(match[1])
    parsed['cpu_rounding_uncertainty_seconds'] = 0.02
    return parsed


def interpolate(samples, timestamp):
    if timestamp <= samples[0]['monotonic_ns']:
        return samples[0]['sampled_tree_cpu_seconds']
    for before, after in zip(samples, samples[1:]):
        if timestamp <= after['monotonic_ns']:
            fraction = (timestamp - before['monotonic_ns']) / (after['monotonic_ns'] - before['monotonic_ns'])
            return before['sampled_tree_cpu_seconds'] + fraction * (
                after['sampled_tree_cpu_seconds'] - before['sampled_tree_cpu_seconds'])
    return samples[-1]['sampled_tree_cpu_seconds']


def interval_metrics(samples, start, end):
    if end <= start:
        raise RuntimeError('Phase timestamps are not increasing.')
    values = [item for item in samples if start <= item['monotonic_ns'] <= end]
    duration = (end - start) / 1e9
    cpu = max(0, interpolate(samples, end) - interpolate(samples, start))
    result = {
        'wall_seconds': duration, 'sample_count': len(values),
        'sampled_cpu_seconds': cpu, 'sampled_mean_cpu_percent_one_core': cpu / duration * 100,
    }
    for key in ('rss', 'footprint'):
        field = f'aggregate_{key}_bytes'
        result[f'peak_aggregate_{key}_mib'] = max(item[field] for item in values) / MIB if values else None
        integral = 0
        for before, after in zip(samples, samples[1:]):
            elapsed = max(0, min(end, after['monotonic_ns']) - max(start, before['monotonic_ns'])) / 1e9
            integral += before[field] * elapsed
        result[f'mean_aggregate_{key}_mib'] = integral / duration / MIB
    result['sampled_peak_live_processes'] = max((item['live_process_count'] for item in values), default=0)
    result['observed_concurrency_lifetime_footprint_mib'] = max(
        (item.get('observed_concurrency_lifetime_footprint_bytes', 0) for item in values), default=0) / MIB
    return result


def validate_phases(messages, start, end):
    phases = {}
    process_ids = set()
    for message in messages:
        if message.get('benchmark_version') != 1:
            raise RuntimeError(f'Unknown benchmark protocol: {message}')
        if message.get('type') != 'phase':
            continue
        name, state = message.get('phase'), message.get('state')
        if name not in PHASES or state not in ('start', 'end'):
            raise RuntimeError(f'Unknown benchmark phase: {message}')
        values = phases.setdefault(name, {})
        if state in values:
            raise RuntimeError(f'Duplicate phase marker: {name} {state}')
        values[state] = int(message['monotonic_ns'])
        process_ids.add(message['pid'])
    if len(process_ids) != 1:
        raise RuntimeError(f'Phase markers do not identify one main GUI process: {process_ids}')
    previous = start
    for name in PHASES:
        values = phases.get(name, {})
        if not (previous <= values.get('start', -1) < values.get('end', -1) <= end):
            raise RuntimeError(f'Missing/out-of-order phase or incompatible monotonic clock: {name}: {values}')
        previous = values['end']
    return phases, process_ids.pop()


def terminate_group(process):
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def measure(app, label, destination, args, api):
    if input_fingerprint(args.input) != args.expected_input_fingerprint:
        raise RuntimeError('Input changed before trial; refusing an unequal-input comparison.')
    destination.mkdir(parents=True, exist_ok=False)
    output_parent = destination / 'converted'
    output_parent.mkdir()
    binary = app / 'Contents/MacOS/ProtoCompile'
    time_log = destination / 'system-time.txt'
    invocation = ['/usr/bin/time', '-l', '-p', '-o', str(time_log), str(binary),
                  '--performance-test', str(args.input), str(output_parent), args.runtime]
    messages, stdout_lines, parse_errors = [], [], []
    observer_cpu_start = time.process_time()
    begin = monotonic_ns()
    with (destination / 'stderr.log').open('w') as errors:
        process = subprocess.Popen(invocation, stdout=subprocess.PIPE, stderr=errors,
                                   text=True, bufsize=1, start_new_session=True)

        def read_stdout():
            for line in process.stdout:
                received = monotonic_ns()
                stdout_lines.append(line)
                try:
                    message = json.loads(line)
                    message['observer_received_ns'] = received
                    messages.append(message)
                except (ValueError, TypeError) as error:
                    parse_errors.append(str(error))

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        sampler = TreeSampler(api, process.pid)
        sampler.samples.append(dict(monotonic_ns=begin, sampled_tree_cpu_seconds=0,
                                    aggregate_rss_bytes=0, aggregate_footprint_bytes=0,
                                    live_process_count=0, pids=[]))
        deadline = begin + int(args.timeout * 1e9)
        observed_exit = None
        next_sample = begin
        try:
            while True:
                now = monotonic_ns()
                if now > deadline:
                    raise RuntimeError('GUI benchmark timed out; this trial is invalid.')
                sampler.sample()
                if process.poll() is not None and observed_exit is None:
                    observed_exit = monotonic_ns()
                if observed_exit and now >= begin + args.window_seconds * 1e9:
                    break
                next_sample += int(args.interval_ms * 1e6)
                wait = (next_sample - monotonic_ns()) / 1e9
                if wait > 0:
                    time.sleep(wait)
                else:
                    next_sample = monotonic_ns()
            reader.join(timeout=2)
        finally:
            terminate_group(process)
            reader.join(timeout=2)
            (destination / 'stdout.jsonl').write_text(''.join(stdout_lines), encoding='utf-8')
            (destination / 'samples.jsonl').write_text(''.join(json.dumps(item) + '\n'
                                                       for item in sampler.samples), encoding='utf-8')
            write_json(destination / 'events.json', messages)
    observer_cpu = time.process_time() - observer_cpu_start
    if process.returncode != 0:
        raise RuntimeError(f'GUI/time exited {process.returncode}; see {destination}/stderr.log')
    if parse_errors:
        raise RuntimeError(f'Non-NDJSON benchmark stdout: {parse_errors}')
    if observed_exit > begin + args.window_seconds * 1e9:
        raise RuntimeError('Trial exceeded the common observation window; increase --window-seconds for BOTH variants.')
    phases, gui_pid = validate_phases(messages, begin, observed_exit)
    terminal = [item for item in messages if item.get('type') == 'result']
    if len(terminal) != 1 or terminal[0].get('status') != 'passed':
        raise RuntimeError(f'GUI driver did not confirm success: {terminal}')
    output = Path(terminal[0]['output_path']).resolve()
    if output.parent != output_parent.resolve():
        raise RuntimeError('GUI driver output is not in this trial export directory.')
    output_digest = output_fingerprint(output)
    if any(output_digest[key] != terminal[0].get(key) or output_digest[key] <= 0
           for key in ('proto_count', 'java_count')):
        raise RuntimeError('GUI driver counts do not match actual converted files.')
    if terminal[0].get('runtime_version') != args.runtime:
        raise RuntimeError('GUI driver used a different target runtime.')
    system = parse_system_time(time_log.read_text())
    final_input = input_fingerprint(args.input)
    if final_input != args.expected_input_fingerprint:
        raise RuntimeError('Input changed during trial; this measurement is invalid.')
    phase_metrics = {name: interval_metrics(sampler.samples, values['start'], values['end'])
                     for name, values in phases.items()}
    phase_metrics['inspect'] = interval_metrics(sampler.samples, phases['inspect_input']['start'],
                                                phases['inspect_output']['end'])
    whole = interval_metrics(sampler.samples, begin, observed_exit)
    window = interval_metrics(sampler.samples, begin, begin + int(args.window_seconds * 1e9))
    window['system_mean_cpu_percent_one_core'] = system['tree_cpu_seconds'] / args.window_seconds * 100
    intervals = [(b['monotonic_ns'] - a['monotonic_ns']) / 1e6
                 for a, b in zip(sampler.samples, sampler.samples[1:])]
    result = {
        'label': label, 'app': str(app), 'main_binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'runtime': args.runtime, 'main_gui_pid': gui_pid, 'visible_gui': True,
        'input_fingerprint_before': args.expected_input_fingerprint,
        'input_fingerprint_after': final_input,
        'output_fingerprint': output_digest,
        'system_time': system, 'whole_lifecycle': whole, 'fixed_window': window,
        'phases': phase_metrics, 'driver_result': terminal[0],
        'processes': list(sampler.records.values()), 'observer_cpu_seconds': observer_cpu,
        'sampling': {'requested_interval_ms': args.interval_ms,
                     'median_interval_ms': statistics.median(intervals), 'max_interval_ms': max(intervals),
                     'mach_timebase': api.timebase,
                     'sampled_minus_system_cpu_seconds': whole['sampled_cpu_seconds'] - system['tree_cpu_seconds']},
    }
    write_json(destination / 'metrics.json', result)
    return result


def median_report(trials):
    def available_median(values):
        values = [value for value in values if value is not None]
        return statistics.median(values) if values else None

    reports = {}
    if len({trial['output_fingerprint']['sha256'] for trial in trials}) != 1:
        raise RuntimeError('Output contents differ across variants/repetitions; refusing an unequal-work comparison.')
    for label in sorted({trial['label'] for trial in trials}):
        selected = [trial for trial in trials if trial['label'] == label]
        values = {
            'tree_cpu_seconds': [trial['system_time']['tree_cpu_seconds'] for trial in selected],
            'fixed_window_cpu_percent': [trial['fixed_window']['system_mean_cpu_percent_one_core'] for trial in selected],
            'peak_aggregate_rss_mib': [trial['whole_lifecycle']['peak_aggregate_rss_mib'] for trial in selected],
            'peak_aggregate_footprint_mib': [trial['whole_lifecycle']['peak_aggregate_footprint_mib'] for trial in selected],
        }
        reports[label] = {metric: {'median': statistics.median(items), 'min': min(items),
                                  'max': max(items), 'trials': items} for metric, items in values.items()}
        reports[label]['phase_medians'] = {
            phase: {metric: available_median(trial['phases'][phase][metric] for trial in selected)
                    for metric in ('sampled_cpu_seconds', 'sampled_mean_cpu_percent_one_core',
                                   'peak_aggregate_rss_mib', 'peak_aggregate_footprint_mib',
                                   'mean_aggregate_rss_mib', 'mean_aggregate_footprint_mib')}
            for phase in (*PHASES, 'inspect')}
    if 'baseline' in reports and 'candidate' in reports:
        comparisons = {}
        for metric in ('tree_cpu_seconds', 'fixed_window_cpu_percent',
                       'peak_aggregate_rss_mib', 'peak_aggregate_footprint_mib'):
            baseline = reports['baseline'][metric]['median']
            candidate = reports['candidate'][metric]['median']
            baseline_min = reports['baseline'][metric]['min']
            candidate_max = reports['candidate'][metric]['max']
            comparisons[metric] = {
                'reduction_percent': (1 - candidate / baseline) * 100 if baseline else None,
                'at_least_50_percent_reduction': bool(baseline and candidate <= baseline * 0.5),
                'conservative_observed_range_reduction_percent':
                    (1 - candidate_max / baseline_min) * 100 if baseline_min else None,
                'all_observed_ranges_support_50_percent':
                    bool(baseline_min and candidate_max <= baseline_min * 0.5),
                'paired_round_reduction_percent': [(1 - candidate_value / baseline_value) * 100
                                                  if baseline_value else None
                                                  for baseline_value, candidate_value in zip(
                    reports['baseline'][metric]['trials'], reports['candidate'][metric]['trials'])],
            }
        reports['comparison'] = comparisons
    return reports


def probe(api):
    before = api.read(os.getpid())['cpu_seconds']
    started = time.process_time()
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        pass
    measured = api.read(os.getpid())['cpu_seconds'] - before
    expected = time.process_time() - started
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.2)'])
    try:
        child_found = child.pid in api.children(os.getpid())
    finally:
        child.wait()
    if abs(measured - expected) > 0.01 or not child_found:
        raise RuntimeError('libproc probe failed CPU-unit or child-discovery validation.')
    with tempfile.TemporaryDirectory(prefix='proto-time-probe-') as folder:
        output = Path(folder) / 'time.txt'
        child_code = 'import time\nt=time.process_time()\nwhile time.process_time()-t<0.2: pass'
        parent_code = f'import subprocess,sys; subprocess.run([sys.executable,"-c",{child_code!r}],check=True)'
        result = subprocess.run(['/usr/bin/time', '-l', '-p', '-o', str(output),
                                 sys.executable, '-c', parent_code],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f'/usr/bin/time probe failed; run outside sandbox: {result.stderr}')
        calibration = parse_system_time(output.read_text())
        if calibration['tree_cpu_seconds'] < 0.18:
            raise RuntimeError(f'System time did not include descendant CPU: {calibration}')
    print(json.dumps({'status': 'passed', 'mach_timebase': api.timebase,
                      'libproc_cpu_seconds': measured, 'process_time_seconds': expected,
                      'child_discovery': child_found, 'system_tree_cpu_calibration': calibration}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true', help='Validate APIs/units/time accounting without launching a GUI')
    parser.add_argument('--app', type=Path, help='Measure one instrumented app')
    parser.add_argument('--label', default='sample')
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--runtime', choices=('3.25.9', '3.6.1'), default='3.25.9')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--warmups', type=int, default=1)
    parser.add_argument('--interval-ms', type=int, choices=(1, 2, 10, 20), default=2)
    parser.add_argument('--window-seconds', type=float, default=10)
    parser.add_argument('--timeout', type=float, default=45)
    args = parser.parse_args()
    api = ProcessAPI()
    if args.probe:
        probe(api)
        return
    if args.app and (args.baseline or args.candidate):
        parser.error('Use --app or --baseline/--candidate, not both.')
    apps = [(args.label, args.app)] if args.app else [('baseline', args.baseline), ('candidate', args.candidate)]
    apps = [(label, app.resolve()) for label, app in apps if app]
    if not apps or args.input is None or args.output_dir is None:
        parser.error('Provide app path(s), --input, and --output-dir.')
    if args.rounds < 1 or args.warmups < 0 or not 5 <= args.window_seconds < args.timeout:
        parser.error('Require rounds>=1, warmups>=0, and 5<=window-seconds<timeout.')
    args.input = args.input.resolve()
    args.expected_input_fingerprint = input_fingerprint(args.input)
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if (args.output_dir / 'report.json').exists():
        parser.error('Output already has report.json; choose a fresh experiment directory.')
    metadata = {
        'schema_version': 1, 'scenario': 'visible GUI launch, idle 2s, input selection, output selection, convert, after 2s, shutdown',
        'host': {'system': platform.platform(), 'machine': platform.machine(), 'logical_cpus': os.cpu_count(),
                 'load_average_before': os.getloadavg()},
        'input': str(args.input), 'input_fingerprint': args.expected_input_fingerprint,
        'runtime': args.runtime, 'rounds': args.rounds,
        'warmups_per_variant': args.warmups, 'interval_ms': args.interval_ms,
        'observation_window_seconds': args.window_seconds,
        'definitions': {
            'cpu': 'user+system CPU for GUI and descendants; 100% is one logical core; system time is authoritative whole-tree total',
            'fixed_window': 'same duration from external launch; samples after full app exit have zero live memory/CPU increment',
            'memory': 'maximum concurrent sum over sampled GUI+descendants; RSS and physical footprint reported separately',
            'phase_cpu': 'interpolated sampled cumulative own CPU per process; never adds child CPU fields and double-counts descendants',
        },
        'limitations': [
            'Processes entirely between samples and each process final CPU tail can be missed; compare against /usr/bin/time tree total.',
            'Peak aggregate memory is a sampled lower bound; summed RSS can count shared mappings more than once.',
            'Summed physical footprint follows per-process kernel ledgers; not a deduplicated measure of total system RAM.',
            'v4 lifetime footprint peaks provide a conservative estimate for observed concurrent PID sets, not a rigorous bound for completely missed processes or exit tails.',
            '/usr/bin/time memory peaks are single-process maxima, not simultaneous aggregate process-tree peaks.',
            'Per-phase CPU is sampled and interpolated; very short phases may have no memory sample.',
            'WindowServer/other system services are not descendants and are not included; same visible GUI/window geometry is required.',
            'System time prints hundredths of a second; user+sys sum has up to about 0.02 s rounding uncertainty.',
            'The common visible-GUI performance driver polls every 40 ms; its small CPU cost is included for both variants, not subtracted.',
            'Warmup trials are retained but excluded from comparison medians; warm-run results do not prove first cold-launch improvements.',
            'Results apply to this host, fixture, runtime and observed workflow; not proof of every workload or macOS 13 compatibility.',
        ],
    }
    metadata['input_sha256'] = args.expected_input_fingerprint['sha256']
    write_json(args.output_dir / 'experiment.json', metadata)
    trials, order = [], []
    number = 0
    for warmup in range(args.warmups):
        for label, app in apps:
            number += 1
            print(f'WARMUP {label} {warmup + 1}', flush=True)
            measure(app, label, args.output_dir / f'{number:03d}-warmup-{label}', args, api)
    for round_number in range(args.rounds):
        sequence = apps if round_number % 2 == 0 else list(reversed(apps))
        for label, app in sequence:
            number += 1
            order.append(label)
            print(f'MEASURE {label} round={round_number + 1}', flush=True)
            trial = measure(app, label, args.output_dir / f'{number:03d}-{label}', args, api)
            trials.append(trial)
            print(json.dumps({'label': label, 'system_cpu_s': trial['system_time']['tree_cpu_seconds'],
                              'peak_rss_mib': trial['whole_lifecycle']['peak_aggregate_rss_mib'],
                              'peak_footprint_mib': trial['whole_lifecycle']['peak_aggregate_footprint_mib']}, ensure_ascii=False), flush=True)
    report = dict(metadata, measured_order=order, summary=median_report(trials), trials=trials)
    report['host']['load_average_after'] = os.getloadavg()
    write_json(args.output_dir / 'report.json', report)
    print('REPORT ' + str(args.output_dir / 'report.json'), flush=True)


if __name__ == '__main__':
    main()
