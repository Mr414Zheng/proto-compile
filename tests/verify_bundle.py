#!/usr/bin/env python3
"""独立验证已构建的应用；非本机架构仅验证包结构，不冒充运行验收。"""

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile


MACHO_MAGICS = {bytes.fromhex(value) for value in (
    'feedface', 'cefaedfe', 'feedfacf', 'cffaedfe',
    'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca',
)}
RUNTIMES = {'3.25.9': 'libprotoc 25.9', '3.6.1': 'libprotoc 3.6.1'}
APP_VERSION = '1.4.1'
APP_BUILD = '7'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(arguments, **kwargs):
    result = subprocess.run([str(arg) for arg in arguments], capture_output=True,
                            text=True, timeout=60, **kwargs)
    require(result.returncode == 0,
            f'命令失败 ({result.returncode}): {arguments[0]}\n{result.stdout}\n{result.stderr}')
    return result


def verify_binary_dependencies(app, arch):
    count = 0
    for path in app.rglob('*'):
        if path.is_symlink():
            require(path.resolve().is_relative_to(app), f'符号链接指向应用外部：{path}')
            require(path.exists(), f'符号链接目标不存在：{path}')
            continue
        if not path.is_file():
            continue
        with path.open('rb') as source:
            if source.read(4) not in MACHO_MAGICS:
                continue
        count += 1
        actual_arch = command(['/usr/bin/lipo', '-archs', path]).stdout.strip()
        require(actual_arch == arch, f'架构不符：{path}: {actual_arch}，期望 {arch}')
        for line in command(['/usr/bin/otool', '-L', path]).stdout.splitlines()[1:]:
            library = line.strip().split(' (', 1)[0]
            require(library.startswith(('@', '/usr/lib/', '/System/Library/')),
                    f'非系统绝对动态库依赖：{path}: {library}')
        minimum_versions = []
        for block in command(['/usr/bin/otool', '-l', path]).stdout.split('Load command '):
            if re.search(r'^\s*cmd LC_BUILD_VERSION\s*$', block, re.MULTILINE):
                version = re.search(r'^\s*minos ([\d.]+)\s*$', block, re.MULTILINE)
            elif re.search(r'^\s*cmd LC_VERSION_MIN_MACOSX\s*$', block, re.MULTILINE):
                version = re.search(r'^\s*version ([\d.]+)\s*$', block, re.MULTILINE)
            else:
                continue
            require(version is not None, f'无法读取二进制最低系统：{path}')
            minimum = tuple(int(part) for part in version[1].split('.'))
            minimum += (0,) * (3 - len(minimum))
            require(minimum <= (13, 0, 0), f'二进制最低系统高于 macOS 13：{path}: {version[1]}')
            minimum_versions.append(minimum)
        require(bool(minimum_versions), f'二进制缺少 macOS 最低系统声明：{path}')
    require(count > 0, '应用中没有可验证的 Mach-O 文件')
    print(f'PASS {count} 个 Mach-O 均为 {arch}，动态库使用系统/相对路径，符号链接位于包内')
    print('PASS 全部 Mach-O 的最低系统声明均不高于 macOS 13.0（不替代真机运行验收）')


def verify_output(result, parent, language):
    """核对实际文件、语言与计数，防止错误复用另一语言的预检产物。"""
    require(result.get('output_language') == language, '结果导出语言不符')
    output = Path(result['output_path']).resolve()
    require(output.parent == parent.resolve() and output.is_dir(), '结果不在选择的导出父目录中')
    require({path.name for path in output.iterdir()} == {language, 'proto'},
            f'结果必须仅含 {language}/proto')
    require(result['proto_count'] == len(list((output / 'proto').rglob('*.proto'))) > 0,
            'Proto 数量不符或为空')
    generated = [path for path in (output / language).rglob('*') if path.is_file()]
    if language == 'java':
        require(all(path.suffix == '.java' for path in generated), 'Java 目录包含非 Java 产物')
        require(result.get('java_count') == len(generated) > 0 and result.get('objc_count') == 0,
                'Java 数量不符或混入 Objective-C 计数')
    else:
        headers = {path.relative_to(output / language).as_posix()[:-2]
                   for path in generated if path.name.endswith('.pbobjc.h')}
        sources = {path.relative_to(output / language).as_posix()[:-2]
                   for path in generated if path.name.endswith('.pbobjc.m')}
        require(bool(headers) and headers == sources and len(generated) == len(headers) * 2,
                'Objective-C 头文件与实现文件未成对或混入其他产物')
        require(result.get('objc_count') == len(generated) and result.get('java_count') == 0,
                'Objective-C 数量不符或混入 Java 计数')
    require(result.get('generated_count') == len(generated), '总生成文件数量不符')
    return output


def engine_request(engine, environment, parent, paths, kind, task_id, target_runtime, language):
    request = dict(protocol_version=1, task_id=task_id, action='convert', input_kind=kind,
                   input_paths=[str(path) for path in paths], include_paths=[],
                   output_parent=str(parent), exclude_opt_0081=True, target_runtime=target_runtime,
                   output_language=language)
    process = command([engine], input=json.dumps(request), env=environment, cwd=parent.parent)
    require(not process.stderr, f'引擎标准错误输出：{process.stderr}')
    messages = [json.loads(line) for line in process.stdout.splitlines()]
    require(bool(messages), '引擎没有 NDJSON 响应')
    require(all(message.get('protocol_version') == 1 and message.get('task_id') == task_id
                for message in messages), '引擎协议版本或任务 ID 不匹配')
    result = messages[-1]
    require(result.get('type') == 'result', f'引擎没有成功结果：{result}')
    require(result.get('compiler_version') == RUNTIMES[target_runtime], f'编译器版本不符：{result}')
    require(result.get('runtime_version') == target_runtime, f'运行库版本不符：{result}')
    verify_output(result, parent, language)
    return result


def verify(app, arch, html=None):
    require(app.is_dir() and app.suffix == '.app', f'不是应用包目录：{app}')
    with tempfile.TemporaryDirectory(prefix='独立验收 bundle 空格 ') as temporary:
        folder = Path(temporary).resolve()
        relocated = folder / app.name
        shutil.copytree(app, relocated, symlinks=True)
        info = plistlib.loads((relocated / 'Contents/Info.plist').read_bytes())
        require(info.get('CFBundleShortVersionString') == APP_VERSION, f'版本必须为 {APP_VERSION}')
        require(info.get('CFBundleVersion') == APP_BUILD, f'构建号必须为 {APP_BUILD}')
        require(info.get('LSMinimumSystemVersion') == '13.0', '最低系统必须为 macOS 13.0')
        print(f'PASS 版本 {APP_VERSION}、构建号 {APP_BUILD}、最低系统声明 macOS 13.0')
        verify_binary_dependencies(relocated, arch)
        command(['/usr/bin/codesign', '--verify', '--deep', '--strict', relocated])
        print('PASS 移动到中文空格路径后，深层严格签名验证通过')
        if platform.machine() != arch:
            print(f'GAP 当前主机为 {platform.machine()}；{arch} 仅完成静态包检查，仍需对应真机运行验收')
            return
        environment = dict(os.environ)
        for key in list(environment):
            if key.startswith(('DYLD_', 'PYTHON', '_PYI')) or key == '_MEIPASS2':
                del environment[key]
        environment.update(PATH='/nonexistent', PYTHONHOME='/does/not/exist',
                           PYTHONPATH='/does/not/exist', TMPDIR=str(folder), LANG='zh_CN.UTF-8')
        engine = relocated / 'Contents/Helpers/ConversionEngine.app/Contents/MacOS/ConversionEngine'
        parent = folder / '导出父目录'
        parent.mkdir()
        source = folder / '协议 source.proto'
        source.write_text('syntax="proto3"; import "google/protobuf/empty.proto"; '
                          'message Portable { google.protobuf.Empty empty = 1; }', encoding='utf-8')
        if html is not None:
            copied_html = folder / html.name
            shutil.copy2(html, copied_html)
        command([relocated / 'Contents/MacOS/ProtoCompile', '--smoke-test'],
                env=environment, cwd=folder)
        print('PASS 移动后的 Swift 应用模型/协议 smoke test（不代表原生面板人工验收）')
        output_paths = set()
        for language in ('java', 'objc'):
            for runtime in RUNTIMES:
                result = engine_request(engine, environment, parent, [source], 'proto',
                                        f'bundle-proto-{language}-{runtime}', runtime, language)
                require(result['proto_count'] == 1 and result['generated_count'] == (1 if language == 'java' else 2),
                        '标准 protobuf 依赖不能导出为用户文件')
                require(result['output_path'] not in output_paths, '不同转换复用了同一结果目录')
                output_paths.add(result['output_path'])
                print(f'PASS {language} {runtime} / {RUNTIMES[runtime]}：隔离 PATH/Python 环境后完成真实 Proto 转换')
                if html is None:
                    continue
                result = engine_request(engine, environment, parent, [copied_html], 'html',
                                        f'bundle-html-{language}-{runtime}', runtime, language)
                require(result['output_path'] not in output_paths, '不同转换复用了同一结果目录')
                output_paths.add(result['output_path'])
                print(f'PASS {language} {runtime} HTML：{result["proto_count"]} Proto / {result["generated_count"]} 生成文件')
                integration = command([relocated / 'Contents/MacOS/ProtoCompile', '--integration-test',
                                       copied_html, parent, runtime, language], env=environment, cwd=folder)
                summary = json.loads(integration.stdout)
                require(summary.get('status') == 'passed', 'Swift 应用端到端转换失败')
                for field in ('proto_count', 'java_count', 'objc_count', 'generated_count', 'output_language'):
                    require(summary.get(field) == result.get(field), f'Swift 与引擎结果不一致：{field}')
                require(summary.get('compiler_version') == RUNTIMES[runtime]
                        and summary.get('runtime_version') == runtime, 'Swift 的编译器/运行库目标不符')
                require(summary.get('application_version') == APP_VERSION
                        and summary.get('application_build') == APP_BUILD, 'Swift 读取的应用版本不符')
                output = verify_output(summary, parent, language)
                require(str(output) not in output_paths, 'Swift 复用了引擎直接转换的结果目录')
                output_paths.add(str(output))
                print(f'PASS {language} {runtime}：搬移后的 Swift AppModel → 预检 → 转换完整链路，语言、版本和 runtime 正确')
        print(f'GAP 当前系统 {platform.mac_ver()[0]}；最低 macOS 13 的运行兼容性仍需该系统真机验收')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path, help='已构建的 .app 路径')
    parser.add_argument('--arch', choices=('arm64', 'x86_64'), required=True)
    parser.add_argument('--html', type=Path, help='可选的真实 HTML 验收输入')
    args = parser.parse_args()
    verify(args.app.resolve(), args.arch, args.html.resolve() if args.html else None)


if __name__ == '__main__':
    main()
