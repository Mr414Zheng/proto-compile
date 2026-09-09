import argparse
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import ProtoError
from .extract import extract, write_sources
from .normalize import normalize
from .repairs import DEFAULT_REPAIRS, repair_sources
from .toolchain import find_protoc, run, setup, RUNTIME_VERSION, VERSION


@contextmanager
def output_transaction(destination):
    destination = destination.expanduser().absolute()
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise ProtoError(f'输出目录必须不存在或为空，不覆盖已有文件: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.proto-build-', dir=destination.parent) as temp:
        staged = Path(temp) / 'result'
        staged.mkdir()
        yield staged
        # 完成编译后再次检查，避免长时间构建期间覆盖用户新放入的文件。
        if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
            raise ProtoError(f'构建期间输出目录发生变化，取消发布: {destination}')
        if destination.exists():
            destination.rmdir()
        os.rename(staged, destination)


def compile_files(protoc, files, includes, java, proto_output=None):
    java.mkdir(parents=True, exist_ok=True)
    roots = []
    bundled = protoc.parent.parent / 'include'
    for root in includes + ([bundled] if bundled.is_dir() else []):
        root = root.resolve()
        if not root.is_dir():
            raise ProtoError(f'找不到 include 目录: {root}')
        if root not in roots:
            roots.append(root)
    logical = {}
    for path in files:
        name = next((path.relative_to(root).as_posix() for root in roots if path.is_relative_to(root)), None)
        if name is None:
            raise ProtoError(f'输入文件不在任何 -I 目录内: {path}')
        resolved = next((root / name for root in roots if (root / name).is_file()), None)
        if resolved is None or resolved.resolve() != path:
            raise ProtoError(f'include 路径遮蔽输入文件: {path} ({name})')
        if name in logical and logical[name] != path:
            raise ProtoError(f'输入 proto 路径冲突: {name}')
        logical[name] = path
    if proto_output is not None:
        proto_output.mkdir(parents=True, exist_ok=True)
        for name, path in logical.items():
            target = proto_output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    command = [protoc] + [f'-I{root}' for root in roots]
    command += [f'--java_out={java}']
    # 使用独立工作目录和参数数组，文件名中的空格不会被 shell 拆分。
    command += [str(logical[name]) for name in sorted(logical)]
    return run(command, cwd=roots[0])


def prepare_html(source, args):
    sources = extract(source)
    selected = {name: item for name, item in sources.items() if name.endswith('.proto')}
    # 按本项目已确认的范围，默认排除历史加密协议，保留其他加密子结构。
    if selected.pop('his_encrypt_data.proto', None) is not None:
        print('已排除 OPT_0081: his_encrypt_data.proto')
    repaired, repairs = (selected, []) if args.no_repairs else repair_sources(
        selected, args.repairs or DEFAULT_REPAIRS, strict=bool(args.repairs))
    java_package = '' if args.no_java_package else (args.java_package or '')
    normalized, _ = normalize(repaired, java_package)
    for change in repairs:
        print(f'协议修正: {change["file"]}: {change.get("reason", change["kind"])}')
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
    if args.exclude_opt and not html:
        raise ProtoError('--exclude-opt 仅用于 HTML')
    if html and args.includes:
        raise ProtoError('HTML 模式从文档内解析依赖；-I 用于普通 proto 编译')
    protoc, version = find_protoc(args.protoc)
    with output_transaction(Path(args.output)) as staged:
        if html:
            sources, normalized = prepare_html(source, args)
            write_sources(normalized, staged / 'proto')
            files = [(staged / 'proto' / name).resolve() for name in normalized]
            includes = [staged / 'proto']
        else:
            if source.is_dir():
                files = sorted(p.resolve() for p in source.rglob('*.proto') if p.is_file())
                includes = [Path(p).expanduser().resolve() for p in args.includes] + [source]
            elif source.suffix == '.proto':
                files = [source]
                includes = [Path(p).expanduser().resolve() for p in args.includes] + [source.parent]
            else:
                raise ProtoError('输入应为 HTML、.proto 文件或 proto 目录')
            if not files:
                raise ProtoError('输入目录中没有 .proto 文件')
        try:
            result = compile_files(protoc, files, includes, staged / 'java',
                                   None if html else staged / 'proto')
        except ProtoError as exc:
            if html:
                origins = '\n'.join(f'{s.name}: {", ".join(s.titles)}' for s in sources.values() if s.name in str(exc))
                raise ProtoError(f'{exc}\n文档来源:\n{origins}') from exc
            raise
        java_count = len(list((staged / 'java').rglob('*.java')))
        warnings = result.stderr.strip().replace(str(staged) + '/', '')
    print(f'成功编译 {len(files)} 个 proto，生成 {java_count} 个 Java 文件: {Path(args.output).absolute()}')
    print(f'编译器: {version}')
    if version == 'libprotoc ' + VERSION:
        print(f'Java 运行库: com.google.protobuf:protobuf-java:{RUNTIME_VERSION}')
    if warnings:
        print(warnings)


def main(argv=None):
    parser = argparse.ArgumentParser(description='从 TiddlyWiki 协议文档或 proto 文件生成 Java 源码')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('setup', help=f'下载并校验 protoc {VERSION}（macOS）')
    extraction = commands.add_parser('extract', help='从 HTML 提取并规范化 proto，仅输出 .proto')
    extraction.add_argument('input')
    extraction.add_argument('-o', '--output', default='output/proto')
    extraction.set_defaults(java_package=None, no_java_package=True, no_repairs=False, repairs=None)
    compilation = commands.add_parser('compile', help='编译 HTML、proto 文件或目录')
    compilation.add_argument('input')
    compilation.add_argument('-o', '--output', default='output', help='输出根目录，默认 output；仅生成 java/ 和 proto/')
    compilation.add_argument('-I', '--proto-path', dest='includes', action='append', default=[])
    compilation.add_argument('--protoc', help='指定 protoc 可执行文件')
    java_options = compilation.add_mutually_exclusive_group()
    java_options.add_argument('--java-package', help='显式设置 HTML 生成 Java 根包；默认不声明 Java 包名')
    java_options.add_argument('--no-java-package', action='store_true', help='HTML 生成 Java 不声明包名，跨文件引用使用类名')
    compilation.add_argument('--exclude-opt', action='append', default=[], choices=['OPT_0081'],
                             help='兼容参数：HTML 模式已默认排除 OPT_0081')
    repairs = compilation.add_mutually_exclusive_group()
    repairs.add_argument('--repairs', help='HTML 修正规则 JSON（以源文件 SHA-256 校验），替代内置规则')
    repairs.add_argument('--no-repairs', action='store_true', help='禁用文档已知问题修正，仅规范化命名空间')
    args = parser.parse_args(argv)
    try:
        if args.command == 'setup':
            print(f'protoc 已准备好: {setup()}')
        elif args.command == 'extract':
            _, sources = prepare_html(Path(args.input).expanduser(), args)
            with output_transaction(Path(args.output)) as staged:
                write_sources(sources, staged)
            print(f'已提取 {sum(n.endswith(".proto") for n in sources)} 个 proto: {Path(args.output).absolute()}')
        else:
            build(args)
        return 0
    except (ProtoError, OSError, UnicodeError) as exc:
        parser.exit(1, f'错误: {exc}\n')
