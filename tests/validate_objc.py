"""使用已有官方 Objective-C 运行库源码验证生成文件与序列化，不自动下载依赖。"""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from proto_tool.service import ConversionRequest, convert


def run(arguments):
    subprocess.run([str(argument) for argument in arguments], check=True, timeout=180)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-source', required=True, type=Path, help='官方源码的 objectivec 目录')
    parser.add_argument('--target-runtime', choices=('3.25.9', '3.6.1'), default='3.25.9')
    parser.add_argument('--generated', type=Path, help='可选，额外编译并链接该目录内的全部 pbobjc 文件')
    parser.add_argument('--protoc', type=Path, help='可选，与目标版本匹配的 protoc')
    args = parser.parse_args()
    runtime = args.runtime_source.resolve()
    if not (runtime / 'GPBProtocolBuffers.h').is_file() or not (runtime / 'GPBProtocolBuffers.m').is_file():
        parser.error('需要完整 Objective-C 运行库源码，cpp-only 源码归档不满足要求')
    podspec = runtime.parent / 'Protobuf.podspec'
    match = re.search(r"s\.version\s*=\s*['\"]([^'\"]+)['\"]", podspec.read_text()) if podspec.is_file() else None
    if match is None or match[1] != args.target_runtime:
        parser.error('运行库源码 Protobuf.podspec 版本与 --target-runtime 不匹配')
    with tempfile.TemporaryDirectory(prefix='proto-objc-validation-') as temporary:
        temporary = Path(temporary)
        # 官方运行库仅在 DEBUG 构建强制检查 required；禁用 ARC 与运行库源码要求一致。
        flags = ['xcrun', 'clang', '-fno-objc-arc', '-DDEBUG=1', '-I', runtime]
        library = temporary / 'Protobuf.o'
        run([*flags, '-O1', '-c', runtime / 'GPBProtocolBuffers.m', '-o', library])
        if args.generated:
            generated = args.generated.resolve()
            headers = sorted(generated.rglob('*.pbobjc.h'))
            implementations = sorted(generated.rglob('*.pbobjc.m'))
            if not headers or {p.with_suffix('') for p in headers} != {p.with_suffix('') for p in implementations}:
                parser.error('待验证的 Objective-C 头文件与实现文件必须非空且成对')
            # 在同一编译单元导入全部头文件，验证独立编译可能掩盖的全局符号冲突。
            checker = temporary / 'GeneratedHeaders.m'
            imports = ['#import <Foundation/Foundation.h>']
            for header in headers:
                name = header.relative_to(generated).as_posix().replace('\\', '\\\\').replace('"', '\\"')
                imports.append(f'#import "{name}"')
            imports.append('int main(void) { @autoreleasepool { return 0; } }')
            checker.write_text('\n'.join(imports) + '\n')
            executable = temporary / 'all-generated'
            run([*flags, '-I', generated, checker, *implementations, library,
                 '-framework', 'Foundation', '-o', executable])
            run([executable])
            print(f'PASS: {len(headers)} Objective-C header/implementation pairs compiled and linked', flush=True)
        inputs = temporary / 'inputs'
        inputs.mkdir()
        fixtures = {
            'a.proto': 'syntax="proto2"; package a; option objc_class_prefix="QAA"; enum Mode { OFF=0; ON=1; } message Item { required fixed32 id=1; }',
            'b.proto': 'syntax="proto2"; package b; option objc_class_prefix="QAB"; message Item { required string text=1; }',
            'main.proto': 'syntax="proto2"; package main; option objc_class_prefix="QAM"; import "a.proto"; import "b.proto"; message Main { required a.Item left=1; optional b.Item right=2; optional a.Mode mode=3; oneof selection { string text=4; int32 number=5; } repeated int32 values=6; }',
        }
        for name, content in fixtures.items():
            (inputs / name).write_text(content + '\n')
        result = convert(ConversionRequest('proto', [inputs / 'main.proto'], destination=temporary / 'synthetic',
                                          target_runtime=args.target_runtime, output_language='objc', protoc=args.protoc))
        generated = Path(result.output_path) / 'objc'
        executable = temporary / 'objc-smoke'
        run([*flags, '-I', generated, ROOT / 'tests/ObjCSmoke.m', *sorted(generated.rglob('*.pbobjc.m')),
             library, '-framework', 'Foundation', '-o', executable])
        run([executable])
        print(f'PASS runtime {args.target_runtime} / {result.compiler_version}', flush=True)


if __name__ == '__main__':
    main()
