#!/usr/bin/env python3
"""串行采样真实可见应用的 BLE 切页和解析；区分 App 与 helper，保留原始数据。"""
import argparse
import hashlib
import json
import platform
import plistlib
from pathlib import Path
import statistics
import subprocess
import time

from benchmark_app import ProcessAPI, monotonic_ns


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def trial(app, arguments, destination):
    api = ProcessAPI()
    stdout_path = destination.with_suffix('.jsonl')
    stderr_path = destination.with_suffix('.stderr')
    samples, processes = [], {}
    with stdout_path.open('w') as out, stderr_path.open('w') as err:
        process = subprocess.Popen([str(app / 'Contents/MacOS/ProtoCompile'), '--ble-performance-test', *arguments], stdout=out, stderr=err)
        started = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic() - started > 150:
                    raise RuntimeError('可见应用测量超过150秒')
                queue, seen = [process.pid], set()
                values = []
                while queue:
                    pid = queue.pop()
                    if pid in seen:
                        continue
                    seen.add(pid)
                    queue.extend(api.children(pid))
                    value = api.read(pid)
                    if value is None:
                        continue
                    value['role'] = 'app' if pid == process.pid else 'helper'
                    key = f'{pid}:{value["start_ticks"]}'
                    previous = processes.get(key, {})
                    value['peak_rss_bytes'] = max(previous.get('peak_rss_bytes', 0), value['rss_bytes'])
                    value['peak_footprint_bytes'] = max(previous.get('peak_footprint_bytes', 0), value['footprint_bytes'])
                    current_path = api.path(pid)
                    # fork 后尚未 exec 的子进程可能短暂显示父程序路径，后续采样必须更新。
                    value['path'] = previous.get('path', current_path) if current_path.startswith('<') else current_path
                    processes[key] = value.copy()
                    values.append(value.copy())
                samples.append({"monotonic_ns": monotonic_ns(), "processes": values})
                time.sleep(0.02)
        except BaseException:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            raise
    if process.returncode != 0:
        raise RuntimeError(f'测量失败：{stdout_path}\n{stderr_path.read_text()}')
    rows = [json.loads(line) for line in stdout_path.read_text().splitlines() if line.startswith('{')]
    if not rows or rows[-1].get('status') != 'passed':
        raise RuntimeError(f'缺少成功终态：{stdout_path}')
    result = {'app': str(app), 'arguments': arguments, 'processes': list(processes.values())}
    switch = [r for r in rows if r['phase'] == 'switch']
    if switch:
        first, last = switch[0], switch[-1]
        elapsed = (last['monotonic_ns'] - first['monotonic_ns']) / 1e9
        cpu = last['cpu_s'] - first['cpu_s']
        intervals = [100 * (b['cpu_s'] - a['cpu_s']) / ((b['monotonic_ns'] - a['monotonic_ns']) / 1e9) for a, b in zip(switch, switch[1:])]
        result.update(cpu_s=cpu, wall_s=elapsed, mean_cpu_percent=100*cpu/elapsed,
                      max_250ms_cpu_percent=max(intervals), ready_rss_mib=first['rss_bytes']/1048576,
                      final_rss_mib=rows[-1]['rss_bytes']/1048576,
                      peak_rss_mib=max(p['peak_rss_bytes'] for p in processes.values() if p['role']=='app')/1048576,
                      lifetime_peak_rss_mib=max(r['max_rss_bytes'] for r in rows)/1048576,
                      checkpoints=[{'index': r['index'], 'rss_mib': r['rss_bytes']/1048576} for r in switch if r['index'] % 40 == 0])
    else:
        first = next(r for r in rows if r['phase'] == 'parse_start')
        last = next(r for r in rows if r['phase'] == 'parse_end')
        path = Path(last['report_path'])
        result.update(wall_s=(last['monotonic_ns']-first['monotonic_ns'])/1e9,
                      app_cpu_s=last['cpu_s']-first['cpu_s'],
                      helper_cpu_s=last['reaped_child_cpu_s']-first['reaped_child_cpu_s'],
                      sampled_helper_cpu_s=sum(p['cpu_seconds'] for p in processes.values() if p['role']=='helper' and 'ConversionEngine' in p['path']),
                      report_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), record_count=last['record_count'],
                      decoded_count=last['decoded_count'], warning_count=last['warning_count'], error_count=last['error_count'])
    save(destination.with_suffix('.samples.json'), {'samples': samples, 'summary': result})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-app', type=Path, required=True)
    parser.add_argument('--candidate-app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--protocol', type=Path)
    parser.add_argument('--json-log', type=Path)
    parser.add_argument('--frames-log', type=Path)
    parser.add_argument('--mode', choices=('switch','parse','long'), required=True)
    args=parser.parse_args()
    if args.rounds < 1: parser.error('--rounds必须大于0')
    args.output.mkdir(parents=True, exist_ok=True)
    metadata={'run_status':'running', 'platform':platform.platform(), 'mode':args.mode, 'rounds':args.rounds, 'apps':{}}
    for label, app in [('baseline',args.baseline_app),('candidate',args.candidate_app)]:
        info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
        metadata['apps'][label]={'path':str(app.resolve()), 'version':info['CFBundleShortVersionString'],
            'build':info['CFBundleVersion'], 'executable_sha256':hashlib.sha256((app/'Contents/MacOS/ProtoCompile').read_bytes()).hexdigest()}
    save(args.output/'manifest.json',metadata)
    all_results=[]
    cases=[('empty',['switch','empty','40']),('success',['switch','success','40'])]
    if args.mode=='long': cases=[('success200',['switch','success','200'])]
    if args.mode=='parse':
        if not args.protocol or not args.json_log: parser.error('parse需要--protocol和--json-log')
        exported=args.output/'reports'; exported.mkdir(exist_ok=True)
        cases=[('json',['parse',str(args.protocol.resolve()),str(args.json_log.resolve()),'json',str(exported.resolve())])]
        if args.frames_log: cases.append(('frames',['parse',str(args.protocol.resolve()),str(args.frames_log.resolve()),'frames',str(exported.resolve())]))
    for run in range(args.rounds):
        for case, arguments in cases:
            # 轮流先运行基线/候选，避免固定运行顺序偏差。
            variants=[('baseline',args.baseline_app),('candidate',args.candidate_app)]
            if run%2: variants.reverse()
            for variant,app in variants:
                result=trial(app.resolve(), arguments, args.output/f'{variant}-{case}-{run+1}')
                result.update(variant=variant, case=case, run=run+1); all_results.append(result)
                save(args.output/'results.json',all_results)
                print(variant,case,run+1,'CPU',round(result.get('cpu_s',result.get('app_cpu_s',0)),4),'wall',round(result['wall_s'],3),flush=True)
    summary={}
    for case,_ in cases:
        grouped={v:[r for r in all_results if r['case']==case and r['variant']==v] for v in ('baseline','candidate')}
        item={v:{key:statistics.median(r[key] for r in rows) for key in ('cpu_s','wall_s','mean_cpu_percent','ready_rss_mib','final_rss_mib','peak_rss_mib','app_cpu_s','helper_cpu_s') if key in rows[0]} for v,rows in grouped.items()}
        if 'cpu_s' in item['baseline']:
            item['cpu_reduction_percent']=100*(1-item['candidate']['cpu_s']/item['baseline']['cpu_s'])
            item['ready_rss_delta_mib']=item['candidate']['ready_rss_mib']-item['baseline']['ready_rss_mib']
        else:
            hashes={r['report_sha256'] for rows in grouped.values() for r in rows}
            if len(hashes)!=1: raise RuntimeError(f'{case} 输出不一致')
            item['report_sha256']=hashes.pop()
        summary[case]=item
    save(args.output/'summary.json',summary)
    metadata['run_status']='complete';save(args.output/'manifest.json',metadata)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
