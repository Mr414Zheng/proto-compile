import argparse
from pathlib import Path

from . import ProtoError
from .extract import write_sources
from .service import ConversionError, ConversionRequest, compile_files, convert, output_transaction
from .service import prepare_html as prepare_html_service
from .toolchain import setup, RUNTIME_VERSION, TARGET_RUNTIMES


def prepare_html(source, args):
    sources, normalized, warnings, changes, excluded = prepare_html_service(
        source, exclude_opt_0081=not getattr(args, 'include_opt_0081', False),
        java_package='' if args.no_java_package else (args.java_package or ''),
        repairs=args.repairs, no_repairs=args.no_repairs)
    for item in excluded:
        print(f'已排除 {item}')
    for item in warnings:
        print(f'警告: {item}')
    for item in changes:
        print(f'协议修正: {item}')
    return sources, normalized


def build(args):
    source = Path(args.input).expanduser().resolve()
    if not source.exists():
        raise ProtoError(f'输入不存在: {source}')
    html = source.is_file() and source.suffix.lower() in ('.html', '.htm')
    if (args.java_package is not None or args.no_java_package) and not html:
        raise ProtoError('--java-package/--no-java-package 仅用于 HTML；普通 proto 请在源文件设置 option java_package')
    if not html and (args.repairs or args.no_repairs):
        raise ProtoError('--repairs/--no-repairs 仅用于 HTML')
    if (args.exclude_opt or args.include_opt_0081) and not html:
        raise ProtoError('--exclude-opt/--include-opt-0081 仅用于 HTML')
    if html and args.includes:
        raise ProtoError('HTML 模式从文档内解析依赖；-I 用于普通 proto 编译')
    result = convert(ConversionRequest(
        input_kind='html' if html else 'proto', input_paths=[source], include_paths=args.includes,
        destination=Path(args.output), protoc=args.protoc, exclude_opt_0081=not args.include_opt_0081,
        java_package='' if args.no_java_package else (args.java_package or ''),
        repairs=args.repairs, no_repairs=args.no_repairs, target_runtime=args.target_runtime))
    for item in result.excluded:
        print(f'已排除 {item}')
    for item in result.changes:
        print(f'协议修正: {item}')
    print(f'成功编译 {result.proto_count} 个 proto，生成 {result.java_count} 个 Java 文件: {result.output_path}')
    print(f'编译器: {result.compiler_version}')
    print(f'Java 运行库: com.google.protobuf:protobuf-java:{result.runtime_version}')
    for item in result.warnings:
        print(f'警告: {item}')

def main(argv=None):
    parser = argparse.ArgumentParser(description='从 TiddlyWiki 协议文档或 proto 文件生成 Java 源码')
    commands = parser.add_subparsers(dest='command', required=True)
    preparation = commands.add_parser('setup', help='准备并校验指定 Java 运行库的 protoc（macOS）')
    preparation.add_argument('--target-runtime', choices=TARGET_RUNTIMES, default=RUNTIME_VERSION, help='目标 protobuf-java 版本，默认 3.25.9')
    extraction = commands.add_parser('extract', help='从 HTML 提取并规范化 proto，仅输出 .proto')
    extraction.add_argument('input')
    extraction.add_argument('-o', '--output', default='output/proto')
    extraction.set_defaults(java_package=None, no_java_package=True, no_repairs=False, repairs=None)
    compilation = commands.add_parser('compile', help='编译 HTML、proto 文件或目录')
    compilation.add_argument('input')
    compilation.add_argument('-o', '--output', default='output', help='输出根目录，默认 output；仅生成 java/ 和 proto/')
    compilation.add_argument('-I', '--proto-path', dest='includes', action='append', default=[])
    compilation.add_argument('--protoc', help='指定与目标 Java 运行库版本匹配的 protoc 可执行文件')
    compilation.add_argument('--target-runtime', choices=TARGET_RUNTIMES, default=RUNTIME_VERSION, help='目标 protobuf-java 版本，默认 3.25.9；可选 3.6.1 兼容模式')
    java_options = compilation.add_mutually_exclusive_group()
    java_options.add_argument('--java-package', help='显式设置 HTML 生成 Java 根包；默认不声明 Java 包名')
    java_options.add_argument('--no-java-package', action='store_true', help='HTML 生成 Java 不声明包名，跨文件引用使用类名')
    exclusions = compilation.add_mutually_exclusive_group()
    exclusions.add_argument('--include-opt-0081', action='store_true', help='包含默认排除的 OPT_0081 历史加密协议')
    exclusions.add_argument('--exclude-opt', action='append', default=[], choices=['OPT_0081'],
                             help='兼容参数：HTML 模式已默认排除 OPT_0081')
    repairs = compilation.add_mutually_exclusive_group()
    repairs.add_argument('--repairs', help='HTML 修正规则 JSON（以源文件 SHA-256 校验），替代内置规则')
    repairs.add_argument('--no-repairs', action='store_true', help='禁用文档已知问题修正，仅规范化命名空间')
    args = parser.parse_args(argv)
    try:
        if args.command == 'setup':
            print(f'protoc 已准备好: {setup(args.target_runtime)}')
        elif args.command == 'extract':
            _, sources = prepare_html(Path(args.input).expanduser(), args)
            with output_transaction(Path(args.output)) as staged:
                write_sources(sources, staged)
            print(f'已提取 {sum(n.endswith(".proto") for n in sources)} 个 proto: {Path(args.output).absolute()}')
        else:
            build(args)
        return 0
    except (ProtoError, OSError, UnicodeError) as exc:
        details = f'\n{exc.details}' if isinstance(exc, ConversionError) and exc.details != str(exc) else ''
        parser.exit(1, f'错误: {exc}{details}\n')
