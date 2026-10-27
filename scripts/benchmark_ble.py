#!/usr/bin/env python3
"""以隔离进程对照 BLE 服务和大单条解码；私有输入及结果仅写入指定目录。"""

import argparse
import binascii
import dataclasses
import filecmp
import hashlib
import json
from pathlib import Path
import platform
import re
import resource
import statistics
import struct
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(MIB), b''):
            digest.update(block)
    return digest.hexdigest()


def usage_snapshot():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime, int(usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024))


def varint(number):
    encoded = bytearray()
    while number > 127:
        encoded.append((number & 127) | 128)
        number >>= 7
    encoded.append(number)
    return bytes(encoded)


def worker(args):
    # 每个 worker 只导入所选源码树；进程启动、导入和输入准备不计入 CPU/墙钟。
    sys.path.insert(0, str(Path(args.source_root).resolve()))
    from proto_tool.ble_decoder import decode_line
    from proto_tool.ble_schema import Candidate, Field, ProtocolSchema, Type
    from proto_tool.ble_service import BleDecodeRequest, decode_logs

    source_file = Path(sys.modules['proto_tool.ble_decoder'].__file__).resolve()
    if source_file.parent.parent != Path(args.source_root).resolve():
        raise RuntimeError('Worker imported an unexpected source tree: ' + str(source_file))
    if args.worker_case in ('json', 'frames'):
        request = BleDecodeRequest(args.protocol, args.worker_input, args.worker_case, args.output)
        event_counts = {}

        def event(**fields):
            stage = fields['stage']
            event_counts[stage] = event_counts.get(stage, 0) + 1

        before_cpu, _ = usage_snapshot()
        started = time.perf_counter()
        result = decode_logs(request, event=event)
        elapsed = time.perf_counter() - started
        after_cpu, peak_rss = usage_snapshot()
        # 结果读取、TXT 哈希和 JSON 编码均在 CPU/墙钟及 RSS 快照之后。
        return dict(wall_seconds=elapsed, cpu_seconds=after_cpu - before_cpu,
                    peak_rss_bytes=peak_rss, report_path=result.report_path,
                    report_bytes=Path(result.report_path).stat().st_size,
                    report_sha256=sha256(result.report_path),
                    counts={name: getattr(result, name) for name in (
                        'record_count', 'decoded_count', 'warning_count', 'error_count', 'proto_count')},
                    event_counts=event_counts)

    fields = {1: Field('payload', 1, 'bytes', resolved='bytes')}
    message = Type('LargeBytes', (), 'message', fields=fields)
    schema = ProtocolSchema(1, [], {1: [Candidate('up', message)]})
    if args.worker_case == 'zero_1m':
        sentence = '00' * MIB
    elif args.worker_case == 'overlimit_3m':
        sentence = '00' * (3 * MIB)
    elif args.worker_case == 'valid_bytes_1m':
        size = MIB - 4
        sentence = '0a' + varint(size).hex() + 'ab' * size
    else:
        raise ValueError(args.worker_case)
    line = json.dumps(dict(id=1, name='up', sentence=sentence))
    del sentence
    before_cpu, _ = usage_snapshot()
    started = time.perf_counter()
    record = decode_line(line, 'json', schema)
    elapsed = time.perf_counter() - started
    after_cpu, peak_rss = usage_snapshot()
    result = dict(wall_seconds=elapsed, cpu_seconds=after_cpu - before_cpu,
                  peak_rss_bytes=peak_rss, status=record.status,
                  decoded_bytes=len(record.structure.get('data', {}).get('payload', '')) // 2,
                  parse_error=record.structure.get('parse_error', []))
    encoded = json.dumps(dataclasses.asdict(record), ensure_ascii=False, sort_keys=True).encode('utf-8')
    result.update(record_sha256=hashlib.sha256(encoded).hexdigest(), record_bytes=len(encoded))
    return result


def derive_frames(json_log, destination):
    count = 0
    with json_log.open(encoding='utf-8-sig') as source, destination.open('w', encoding='ascii') as output:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line[line.index('{'):])
                sentence = obj['sentence']
                if not isinstance(sentence, str) or len(sentence) % 2 or not re.fullmatch('[0-9A-Fa-f]*', sentence):
                    raise ValueError('Invalid hexadecimal sentence')
                payload = bytes.fromhex(sentence)
                opcode = int(obj['id'])
                header = b'DT' + struct.pack('<HHH', len(payload), binascii.crc_hqx(payload, 0), opcode)
            except (ValueError, KeyError, TypeError, struct.error) as exc:
                raise ValueError(f'Cannot derive a BLE frame from JSON line {number}: {exc}') from exc
            output.write((header + payload).hex().upper() + '\n')
            count += 1
    if not count:
        raise ValueError('JSON log has no records')
    return count


def extract_raw_frames(raw_log, destination):
    count = 0
    pattern = re.compile(r'全部分包拼接完毕:\s*([0-9A-Fa-f]+)\s*$')
    with raw_log.open(encoding='utf-8-sig') as source, destination.open('w', encoding='ascii') as output:
        for line in source:
            match = pattern.search(line)
            if match:
                output.write(match.group(1) + '\n')
                count += 1
    if not count:
        raise ValueError('No fully reassembled frames found in raw log')
    return count


def repeat_log(source, destination, factor):
    # 使用有界缓冲重复输入；worker 的 RSS 不包含派生输入成本。
    with destination.open('wb') as output:
        for _ in range(factor):
            with source.open('rb') as stream:
                last = b''
                for block in iter(lambda: stream.read(MIB), b''):
                    output.write(block)
                    last = block[-1:]
                if last and last != b'\n':
                    output.write(b'\n')


def describe(path, **extra):
    return dict(path=str(path.resolve()), bytes=path.stat().st_size, sha256=sha256(path), **extra)


def summarize(rows):
    return {metric: dict(median=statistics.median(row[metric] for row in rows),
                         min=min(row[metric] for row in rows), max=max(row[metric] for row in rows))
            for metric in ('wall_seconds', 'cpu_seconds', 'peak_rss_bytes')}


def benchmark(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixtures = output / 'private-fixtures'
    fixtures.mkdir(exist_ok=True)
    protocol, json_log = Path(args.protocol).resolve(), Path(args.json_log).resolve()
    derived = fixtures / 'derived-frames.txt'
    count = derive_frames(json_log, derived)
    frames_log = Path(args.frames_log).resolve() if args.frames_log else derived
    inputs = dict(protocol=describe(protocol), json=describe(json_log, records=count),
                  frames=describe(frames_log, provenance='provided' if args.frames_log else 'derived from JSON payloads; not captured BLE frames'))
    jobs = [('json', 'json', json_log), ('frames', 'frames', frames_log)]
    for name, kind, path in list(jobs):
        repeated = fixtures / (name + '-10x.txt')
        repeat_log(path, repeated, 10)
        inputs[name + '_10x'] = describe(repeated, provenance='10 repetitions in original order')
        jobs.append((name + '_10x', kind, repeated))
    if args.raw_log:
        raw_log = Path(args.raw_log).resolve()
        raw_frames = fixtures / 'raw-extracted-frames.txt'
        raw_count = extract_raw_frames(raw_log, raw_frames)
        inputs['raw'] = describe(raw_log)
        inputs['raw_frames'] = describe(raw_frames, records=raw_count,
            provenance='Only hex following 全部分包拼接完毕:, in original order; surrounding Android display log excluded')
        jobs.append(('raw_frames', 'frames', raw_frames))
    for case in ('zero_1m', 'overlimit_3m', 'valid_bytes_1m'):
        jobs.append((case, case, None))
    roots = dict(baseline=str(Path(args.baseline_root).resolve()), candidate=str(ROOT))
    result = dict(benchmark_version=1, platform=platform.platform(), python=sys.version,
                  run_status='running', started_unix_seconds=time.time(),
                  command=[sys.executable, *sys.argv], rounds=args.rounds, sources=roots,
                  source_sha256={version: {path.name: sha256(path) for path in sorted((Path(root) / 'proto_tool').glob('*.py'))}
                                 for version, root in roots.items()},
                  inputs=inputs, cases={},
                  methodology=dict(cpu='RUSAGE_SELF user+system delta around decode_logs/decode_line only; no profiler',
                    wall='perf_counter around decode_logs/decode_line; startup/import/input preparation excluded',
                    rss='RUSAGE_SELF lifetime ru_maxrss, normalized to bytes; fresh worker per case/version/round',
                    snapshot='CPU, wall and peak RSS captured before output hashing or record serialization',
                    cache='OS caches not flushed; versions alternate each round',
                    equivalence='All rounds require equal output hashes and counts; service outputs also compared byte-for-byte'))
    (output / 'benchmark.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for name, kind, path in jobs:
        rows = {version: [] for version in roots}
        for round_index in range(args.rounds):
            order = list(roots) if round_index % 2 == 0 else list(reversed(roots))
            for version in order:
                run_output = output / 'reports' / name / version / str(round_index + 1)
                run_output.mkdir(parents=True, exist_ok=True)
                command = [sys.executable, str(Path(__file__).resolve()), '--worker-case', kind,
                           '--source-root', roots[version], '--protocol', str(protocol), '--output', str(run_output)]
                if path:
                    command += ['--worker-input', str(path)]
                completed = subprocess.run(command, check=True, text=True, capture_output=True)
                rows[version].append(json.loads(completed.stdout))
            print(f'{name}: round {round_index + 1}/{args.rounds}', flush=True)
        hash_key = 'report_sha256' if path else 'record_sha256'
        all_rows = rows['baseline'] + rows['candidate']
        hashes_match = len({row[hash_key] for row in all_rows}) == 1
        if path:
            same_counts = all(row['counts'] == all_rows[0]['counts'] for row in all_rows)
            byte_equal = all(filecmp.cmp(all_rows[0]['report_path'], row['report_path'], shallow=False) for row in all_rows[1:])
        else:
            same_counts = all((row['status'], row['decoded_bytes'], row['parse_error']) ==
                             (all_rows[0]['status'], all_rows[0]['decoded_bytes'], all_rows[0]['parse_error']) for row in all_rows)
            byte_equal = None
        summaries = {version: summarize(values) for version, values in rows.items()}
        reductions = {metric: 100 * (1 - summaries['candidate'][metric]['median'] / summaries['baseline'][metric]['median'])
                      for metric in ('wall_seconds', 'cpu_seconds', 'peak_rss_bytes')}
        result['cases'][name] = dict(runs=rows, summary=summaries, reduction_percent=reductions,
                                    hashes_identical=hashes_match, counts_identical=same_counts,
                                    reports_byte_identical=byte_equal)
        (output / 'benchmark.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if not hashes_match or not same_counts or byte_equal is False:
            raise RuntimeError('Behavior differs in ' + name)
    result['acceptance'] = dict(all_outputs_identical=True,
        valid_large_bytes_message_decoded=all(
            row['status'] == 'decoded' and row['decoded_bytes'] == MIB - 4
            for rows in result['cases']['valid_bytes_1m']['runs'].values() for row in rows),
        large_record_rss_reduction_at_least_70_percent=all(
            result['cases'][case]['reduction_percent']['peak_rss_bytes'] >= 70
            for case in ('zero_1m', 'overlimit_3m', 'valid_bytes_1m')))
    result['run_status'] = 'complete'
    (output / 'benchmark.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({name: case['reduction_percent'] for name, case in result['cases'].items()}, indent=2))
    return 0 if all(result['acceptance'].values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root')
    parser.add_argument('--protocol', required=True)
    parser.add_argument('--json-log')
    parser.add_argument('--frames-log')
    parser.add_argument('--raw-log')
    parser.add_argument('--output', required=True)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--worker-case', choices=('json', 'frames', 'zero_1m', 'overlimit_3m', 'valid_bytes_1m'), help=argparse.SUPPRESS)
    parser.add_argument('--source-root', help=argparse.SUPPRESS)
    parser.add_argument('--worker-input', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_case:
        print(json.dumps(worker(args), ensure_ascii=False))
        return 0
    if not args.baseline_root or not args.json_log or args.rounds < 1:
        parser.error('--baseline-root, --json-log and positive --rounds are required')
    return benchmark(args)


if __name__ == '__main__':
    raise SystemExit(main())
