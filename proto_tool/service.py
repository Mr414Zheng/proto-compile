"""CLI 与桌面进程共用的转换服务，临时结果只在完整成功后发布。"""

import os
import hashlib
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime
from pathlib import Path

from . import ProtoError
from . import prepared
from .extract import Source, extract, safe_name, write_sources
from .normalize import TOKEN, normalize
from .repairs import DEFAULT_REPAIRS, repair_sources
from .toolchain import RUNTIME_VERSION, TARGET_RUNTIMES, find_protoc, run

JAVA_BATCH_FILES = 10
JAVA_BATCH_SOURCE_BYTES = 32 * 1024
OUTPUT_LANGUAGES = ('java', 'objc')


class ConversionError(ProtoError):
    def __init__(self, message, code='CONVERSION_FAILED', suggestion='请检查输入文件、依赖目录和编译器诊断后重试。', file=None, line=None, details=''):
        super().__init__(message)
        self.code = code
        self.suggestion = suggestion
        self.file = file
        self.line = line
        self.details = details or message


class Cancelled(ConversionError):
    def __init__(self):
        super().__init__('转换已取消，未发布输出文件。', 'CANCELLED', '可重新选择输入并再次转换。')


class CancellationToken:
    """阶段之间与子进程运行期间响应取消；发布开始后保留一致的成功结果。"""
    def __init__(self):
        self._cancelled = threading.Event()
        self._publishing = False
        self._published = False

    def cancel(self):
        if not self._publishing and not self._published:
            self._cancelled.set()

    def check(self):
        if self._cancelled.is_set() and not self._publishing and not self._published:
            raise Cancelled()

    @contextmanager
    def publication(self):
        self.check()
        # SIGTERM 处理器只设置标记；这段短临界区不会留下已发布却报告取消的状态。
        self._publishing = True
        try:
            if self._cancelled.is_set():
                raise Cancelled()
            yield
            self._published = True
        finally:
            self._publishing = False

    def run(self, command, cwd=None):
        self.check()
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            while True:
                self.check()
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    self.check()
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


@dataclass
class ConversionRequest:
    input_kind: str
    input_paths: list
    include_paths: list = field(default_factory=list)
    output_parent: object = None
    exclude_opt_0081: bool = True
    action: str = 'convert'
    protoc: object = None
    destination: object = None
    java_package: str = ''
    repairs: object = None
    no_repairs: bool = False
    target_runtime: str = RUNTIME_VERSION
    preparation_output: object = None
    preparation_path: object = None
    output_language: str = 'java'


@dataclass
class ConversionResult:
    output_path: object
    proto_count: int
    java_count: int
    compiler_version: str
    runtime_version: str
    warnings: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    source_files: list = field(default_factory=list)
    preparation_path: object = None
    output_language: str = 'java'
    objc_count: int = 0
    generated_count: int = 0


def describe_change(change):
    if change.get('reason'):
        return f'{change["file"]}: {change["reason"]}'
    if change['kind'] == 'package':
        return f'{change["file"]}: 协议命名空间规范化为 {change["to"]}'
    label = {'repair_import': '补充依赖', 'repair_type': '修正类型', 'enum_value': '规范化枚举值',
             'type_reference': '规范化类型引用', 'enum_default': '修正枚举默认值'}.get(change['kind'], '协议修正')
    return f'{change["file"]}: {label} {change.get("from", "")} → {change.get("to", "")}'


def prepare_html(path, *, exclude_opt_0081=True, java_package='', repairs=None, no_repairs=False, output_language='java'):
    sources = extract(path)
    selected = {name: item for name, item in sources.items() if name.endswith('.proto')}
    excluded = []
    if exclude_opt_0081 and selected.pop('his_encrypt_data.proto', None) is not None:
        excluded.append('OPT_0081: his_encrypt_data.proto')
    if not selected:
        raise ConversionError('排除所选协议后没有可转换的 proto。', 'EMPTY_INPUT')
    warnings = []
    repaired, repair_changes = (selected, []) if no_repairs else repair_sources(
        selected, repairs or DEFAULT_REPAIRS, strict=bool(repairs), warnings=warnings)
    normalized, changes = normalize(repaired, java_package, output_language=output_language)
    return sources, normalized, warnings, [describe_change(c) for c in repair_changes + changes], excluded


def _tokens(path, observed=None, contents=None):
    raw = path.read_bytes()
    text = raw.decode('utf-8-sig')
    if observed is not None:
        observed[str(path)] = hashlib.sha256(raw).hexdigest()
    if contents is not None:
        contents[path] = text
    tokens = [m[0] for m in TOKEN.finditer(text) if not m[0].isspace() and not m[0].startswith(('//', '/*'))]
    if not tokens:
        raise ConversionError(f'输入 proto 为空: {path}', 'EMPTY_INPUT', file=str(path))
    for index, value in enumerate(tokens):
        if value == 'syntax' and index + 2 < len(tokens) and tokens[index + 1] == '=':
            if tokens[index + 2] not in ('"proto2"', '"proto3"', "'proto2'", "'proto3'"):
                raise ConversionError(f'不支持的 proto 语法: {tokens[index + 2]} ({path})', 'UNSUPPORTED_SYNTAX', file=str(path))
    return tokens


def _imports(path, observed=None, contents=None):
    tokens = _tokens(path, observed, contents)
    for index, value in enumerate(tokens):
        if value != 'import':
            continue
        offset = index + 1
        if offset < len(tokens) and tokens[offset] in ('public', 'weak'):
            offset += 1
        if offset < len(tokens) and tokens[offset].startswith(('"', "'")):
            name = tokens[offset][1:-1]
            safe_name(name)
            yield name


def collect_proto_files(files, includes, protoc, cancellation=None, observed=None, bindings=None, contents=None):
    """递归收集所有用户依赖，标准 protobuf 文件仅作为编译器 include。"""
    token = cancellation or CancellationToken()
    roots = []
    bundled = protoc.parent.parent / 'include'
    for root in [Path(p).resolve() for p in includes] + ([bundled.resolve()] if bundled.is_dir() else []):
        if not root.is_dir():
            raise ConversionError(f'找不到 include 目录: {root}', 'MISSING_INCLUDE', file=str(root))
        if root not in roots:
            roots.append(root)
    logical = {}
    pending = deque(files)
    seen = set()
    resolved_names = {}

    def resolve(name):
        if name in resolved_names:
            return resolved_names[name]
        candidates = list(dict.fromkeys((root / name).resolve() for root in roots if (root / name).is_file()))
        if not candidates:
            raise ConversionError(f'缺失 import 依赖: {name}', 'MISSING_IMPORT', '请添加包含该 import 路径的 include 目录。', file=name)
        if len(candidates) > 1:
            raise ConversionError(f'include 路径遮蔽: {name}', 'SHADOWED_IMPORT', '请移除重复依赖或调整 include 目录，确保每个导入路径唯一。', file=name, details='\n'.join(map(str, candidates)))
        resolved_names[name] = candidates[0]
        if bindings is not None:
            bindings[name] = [str(candidates[0])]
        return candidates[0]

    while pending:
        token.check()
        path = Path(pending.popleft()).resolve()
        if path in seen:
            continue
        seen.add(path)
        name = next((path.relative_to(root).as_posix() for root in roots if path.is_relative_to(root)), None)
        if name is None:
            raise ConversionError(f'输入文件不在任何 -I 目录内: {path}', 'MISSING_INCLUDE', file=str(path))
        if resolve(name) != path:
            raise ConversionError(f'include 路径遮蔽输入文件: {path} ({name})', 'SHADOWED_IMPORT', file=str(path))
        standard = bundled.is_dir() and path.is_relative_to(bundled.resolve() / 'google' / 'protobuf')
        if not standard:
            if name in logical and logical[name] != path:
                raise ConversionError(f'输入 proto 路径冲突: {name}', 'SHADOWED_IMPORT', file=name)
            logical[name] = path
        for dependency in _imports(path, observed, contents):
            try:
                dependency_path = resolve(dependency)
            except ConversionError as exc:
                exc.file = str(path)
                exc.details = f'{path}: {dependency}\n{exc.details}'
                raise
            pending.append(dependency_path)
    if not logical:
        raise ConversionError('输入中没有可转换的用户 proto 文件。', 'EMPTY_INPUT')
    return logical, roots


def compile_files(protoc, files, includes, java, proto_output=None, cancellation=None, output_language='java'):
    logical, roots = collect_proto_files(files, includes, protoc, cancellation)
    java.mkdir(parents=True, exist_ok=True)
    if proto_output is not None:
        proto_output.mkdir(parents=True, exist_ok=True)
        for name, path in logical.items():
            target = proto_output / safe_name(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return _compile_collected(protoc, logical, roots, java, cancellation, output_language=output_language)


def _compile_collected(protoc, logical, roots, java, cancellation=None, output_language='java'):
    """依赖已由服务校验，直接生成，避免重复读取与路径解析。"""
    if output_language not in OUTPUT_LANGUAGES:
        raise ConversionError('输出语言必须为 Java 或 Objective-C。', 'INVALID_REQUEST')
    java.mkdir(parents=True, exist_ok=True)
    files = [logical[name] for name in sorted(logical)]
    common = [protoc] + [f'-I{root}' for root in roots]
    command = common + [f'--{output_language}_out={java}'] + list(map(str, files))
    sizes = [path.stat().st_size for path in files]
    if len(files) <= JAVA_BATCH_FILES or sum(sizes) <= JAVA_BATCH_SOURCE_BYTES:
        result = run(command, cwd=roots[0], cancellation=cancellation)
        if output_language == 'objc':
            _validate_objc_symbols(java, cancellation)
        return result
    token = cancellation or CancellationToken()
    # protoc 在写盘前累积生成正文；限制每批入口数和源文本量，降低生成正文峰值。
    # 单份大型协议无法据此拆小，仍交由同一原生生成器完整处理。
    batches, batch, volume = [], [], 0
    for path, size in zip(files, sizes):
        if batch and (len(batch) >= JAVA_BATCH_FILES or volume + size > JAVA_BATCH_SOURCE_BYTES):
            batches.append(batch)
            batch, volume = [], 0
        batch.append(path)
        volume += size
    if batch:
        batches.append(batch)
    with tempfile.TemporaryDirectory(prefix='.proto-batches-', dir=java.parent) as temp:
        temp = Path(temp)
        # 分批会隐藏未互相 import 的同名符号，先全量校验，保留原编译器的全局错误。
        validated = run(common + [f'--descriptor_set_out={temp / "validated.pb"}'] + list(map(str, files)),
                        cwd=roots[0], cancellation=token)
        parse_warnings = set(validated.stderr.splitlines())
        warnings = validated.stderr.splitlines()
        stdout = [validated.stdout]
        outputs, collisions = {}, []

        def generator_diagnostics(text):
            # 旧编译器按当前入口集合推断 unused import，分批可能产生全量编译没有的警告。
            # 输入文件的解析警告统一使用全量校验结果；目标语言后端的原生日志仍完整保留。
            return [line for line in text.splitlines() if line not in parse_warnings
                    and not re.search(r'\.proto:(?:\d+(?::\d+)?:)?\s*warning:', line, re.IGNORECASE)]

        for index, files_in_batch in enumerate(batches):
            token.check()
            target = temp / str(index)
            target.mkdir()
            try:
                result = run(common + [f'--{output_language}_out={target}'] + list(map(str, files_in_batch)),
                             cwd=roots[0], cancellation=token)
            except ProtoError as exc:
                if isinstance(exc, Cancelled):
                    raise
                diagnostic = str(exc).split('\n', 1)
                if len(diagnostic) == 2 and diagnostic[0].startswith('命令失败 ('):
                    remaining = generator_diagnostics(diagnostic[1])
                    details = '\n'.join(warnings + collisions + remaining)
                    raise ProtoError(diagnostic[0] + '\n' + details) from exc
                raise
            warnings.extend(generator_diagnostics(result.stderr))
            stdout.append(result.stdout)
            for path in sorted(target.rglob('*')):
                if not path.is_file():
                    continue
                relative = path.relative_to(target)
                if relative in outputs:
                    collisions.append(f'{relative.as_posix()}: Tried to write the same file twice.')
                else:
                    outputs[relative] = path
        if collisions:
            details = '\n'.join(warnings + collisions)
            raise ProtoError(f'命令失败 (1): {protoc}\n{details}')
        if output_language == 'objc':
            _validate_objc_symbols(temp, token)
        # 各批输出隔离且完整通过后才合并；同名文件即使内容一致也按原生规则拒绝。
        for relative, source in outputs.items():
            token.check()
            destination = java / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
        stderr = '\n'.join(warnings) + ('\n' if warnings else '')
        return subprocess.CompletedProcess(command, 0, ''.join(stdout), stderr)


def _validate_objc_symbols(output, cancellation=None):
    """Objective-C 没有 proto 包命名空间，拒绝生成器未报错的跨文件公开符号冲突。"""
    declarations = re.compile(r'^\s*(?:GPB_FINAL\s+)?@interface\s+(\w+)\s*:|^\s*typedef\s+GPB_ENUM\(\s*(\w+)\s*\)', re.MULTILINE)
    symbols = {}
    for path in sorted(output.rglob('*.pbobjc.h')):
        if cancellation:
            cancellation.check()
        for match in declarations.finditer(path.read_text(encoding='utf-8')):
            name = match[1] or match[2]
            previous = symbols.get(name)
            if previous is not None:
                raise ProtoError(f'Objective-C 符号冲突: {name} ({previous.relative_to(output)} 与 {path.relative_to(output)})；'
                                 '请为源协议设置不同的 option objc_class_prefix')
            symbols[name] = path


def _check_destination(destination):
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise ProtoError(f'输出目录必须不存在或为空，不覆盖已有文件: {destination}')


@contextmanager
def output_transaction(destination, cancellation=None):
    destination = destination.expanduser().absolute()
    _check_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = cancellation or CancellationToken()
    with tempfile.TemporaryDirectory(prefix='.proto-build-', dir=destination.parent) as temp:
        staged = Path(temp) / 'result'
        staged.mkdir()
        yield staged
        with token.publication():
            try:
                _check_destination(destination)
            except ProtoError as exc:
                raise ProtoError(f'构建期间输出目录发生变化，取消发布: {destination}') from exc
            if destination.exists():
                destination.rmdir()
            os.rename(staged, destination)


def _preparation_target(request):
    for name, action in (('preparation_output', 'inspect'), ('preparation_path', 'convert')):
        value = getattr(request, name)
        if value is not None:
            if request.action != action or not Path(value).is_absolute():
                raise ConversionError(f'{name} 仅可用于 {action}，且必须为绝对路径。', 'INVALID_REQUEST')
    if request.preparation_output is None:
        return None
    target = Path(request.preparation_output)
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise ConversionError('预检成果目标必须不存在，且父目录必须已存在。', 'INVALID_REQUEST')
    return target


def _proto_inputs(inputs, include_paths):
    files, automatic = [], []
    for path in inputs:
        if path.is_dir():
            files.extend(sorted(p.resolve() for p in path.rglob('*.proto') if p.is_file()))
            automatic.append(path)
        elif path.suffix.lower() == '.proto':
            files.append(path)
            automatic.append(path.parent)
        else:
            raise ConversionError(f'输入应为 .proto 文件或目录: {path}', 'INVALID_REQUEST', file=str(path))
    if not files:
        raise ConversionError('输入目录中没有 .proto 文件。', 'EMPTY_INPUT')
    return files, [Path(p).expanduser().resolve() for p in include_paths] + automatic


def _snapshot(request, inputs, protoc, version, hashes, bindings, roots, entries):
    return dict(identity=prepared.identity(request, protoc, version), files=dict(hashes),
                bindings=dict(bindings), roots=list(map(str, roots)), entries=entries)


def _unchanged(snapshot, request, inputs, protoc, version, token, ignored=()):
    return (snapshot['identity'] == prepared.identity(request, protoc, version)
            and snapshot['entries'] == prepared.directory_entries(inputs, ignored)
            and all(Path(root).is_dir() for root in snapshot['roots'])
            and snapshot['bindings'] == prepared.binding_state(snapshot['bindings'], [Path(p) for p in snapshot['roots']])
            and snapshot['files'] == prepared.fingerprints(snapshot['files'], token))


def _cached_result(request, snapshot, staged, token):
    if not request.preparation_path:
        return None
    try:
        manifest = prepared.load(request.preparation_path, token, expected_snapshot=snapshot)
        if manifest['snapshot'] != snapshot:
            return None
        fields = manifest['result']
        if fields.get('compiler_version') != snapshot['identity']['compiler_version'] or fields.get('runtime_version') != request.target_runtime:
            return None
        prepared.copy_verified(request.preparation_path, staged, manifest, token)
        return ConversionResult(output_path=None, **fields)
    except (prepared.InvalidPreparation, OSError, UnicodeError, ValueError, TypeError):
        # 缓存不可用时重新校验和生成；取消异常不在此捕获，继续向上传播。
        for name in ('java', 'objc', 'proto'):
            path = staged / name
            if path.exists():
                shutil.rmtree(path)
        return None


def convert(request, event=None, cancellation=None):
    """执行真实预检或转换；预检成果只有在输入与产物摘要全部匹配时复用。"""
    token = cancellation or CancellationToken()

    def progress(stage, message, **counts):
        token.check()
        if event:
            event(stage=stage, message_zh=message, **counts)
        token.check()

    if request.input_kind not in ('html', 'proto') or request.action not in ('inspect', 'convert', 'version'):
        raise ConversionError('不支持的输入类型或操作。', 'INVALID_REQUEST')
    if request.target_runtime not in TARGET_RUNTIMES:
        raise ConversionError('不支持的运行库版本，请选择 3.25.9 或 3.6.1。', 'INVALID_REQUEST')
    if request.output_language not in OUTPUT_LANGUAGES:
        raise ConversionError('输出语言必须为 Java 或 Objective-C。', 'INVALID_REQUEST')
    language_label = 'Java' if request.output_language == 'java' else 'Objective-C'
    preparation_target = _preparation_target(request)
    if request.action == 'version':
        _, version = find_protoc(request.protoc, cancellation=token, target_runtime=request.target_runtime)
        return ConversionResult(None, 0, 0, version, request.target_runtime, output_language=request.output_language)
    progress('reading', '正在读取输入文件')
    inputs = [Path(p).expanduser().resolve() for p in request.input_paths]
    if not inputs:
        raise ConversionError('请至少选择一个输入文件或目录。', 'EMPTY_INPUT')
    for path in inputs:
        if not path.exists():
            raise ConversionError(f'输入不存在: {path}', 'INPUT_NOT_FOUND', file=str(path))
    if request.input_kind == 'html' and (len(inputs) != 1 or not inputs[0].is_file()):
        raise ConversionError('HTML 模式需要一个 HTML 文件。', 'INVALID_REQUEST')
    protoc, version = find_protoc(request.protoc, cancellation=token, target_runtime=request.target_runtime)
    if request.destination:
        destination = Path(request.destination).expanduser().absolute()
    else:
        if request.action == 'convert' and not request.output_parent:
            raise ConversionError('请选择输出父目录。', 'INVALID_REQUEST')
        parent = Path(request.output_parent).expanduser().resolve() if request.output_parent else None
        if parent is not None and not parent.is_dir():
            raise ConversionError(f'输出父目录不存在: {parent}', 'OUTPUT_NOT_FOUND', file=str(parent))
        destination = parent / f'协议转换-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex}' if parent else None
    if request.action == 'convert':
        _check_destination(destination)
    tracking = bool(request.preparation_output or request.preparation_path)
    hashes, bindings, contents = {}, {}, {}
    entries = prepared.directory_entries(inputs) if tracking else {}
    if tracking:
        tracked = [protoc]
        if request.input_kind == 'html':
            tracked += inputs
            if not request.no_repairs:
                tracked.append(Path(request.repairs or DEFAULT_REPAIRS).expanduser().resolve())
        hashes.update(prepared.fingerprints(tracked, token))
    workspace_parent = destination.parent if request.action == 'convert' else (preparation_target.parent if preparation_target else None)
    if workspace_parent:
        workspace_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.proto-build-', dir=workspace_parent) as temp:
        staged = Path(temp) / 'result'
        staged.mkdir()
        warnings, changes, excluded = [], [], []
        progress('parsing', '正在解析协议与依赖')
        logical, roots = {}, []
        if request.input_kind == 'proto':
            files, includes = _proto_inputs(inputs, request.include_paths)
            logical, roots = collect_proto_files(files, includes, protoc, token,
                                                 observed=hashes if tracking else None,
                                                 bindings=bindings if tracking else None, contents=contents)
        snapshot = _snapshot(request, inputs, protoc, version, hashes, bindings, roots, entries) if tracking else None
        progress('validating', '正在校验协议与依赖')
        result = _cached_result(request, snapshot, staged, token) if request.preparation_path else None
        if result is not None and not _unchanged(snapshot, request, inputs, protoc, version, token, (Path(temp),)):
            for name in ('java', 'objc', 'proto'):
                if (staged / name).exists():
                    shutil.rmtree(staged / name)
            result = None
        if result is not None:
            progress('generating', '已校验并复用预检生成成果', processed=result.proto_count, total=result.proto_count)
        else:
            if request.input_kind == 'html':
                sources, normalized, warnings, changes, excluded = prepare_html(
                    inputs[0], exclude_opt_0081=request.exclude_opt_0081, java_package=request.java_package,
                    repairs=request.repairs, no_repairs=request.no_repairs, output_language=request.output_language)
                write_sources(normalized, staged / 'proto')
                files = [(staged / 'proto' / name).resolve() for name in normalized]
                logical, roots = collect_proto_files(files, [staged / 'proto'], protoc, token)
                source_files = [str(inputs[0])]
            else:
                source_files = [str(logical[name]) for name in sorted(logical)]
                write_sources({name: Source(name, contents[path]) for name, path in logical.items()}, staged / 'proto')
            progress('generating', f'正在生成 {language_label} 源码', processed=0, total=len(logical))
            try:
                generated = _compile_collected(protoc, logical, roots, staged / request.output_language, token,
                                               output_language=request.output_language)
            except ProtoError as exc:
                if isinstance(exc, Cancelled):
                    raise
                diagnostic = str(exc).replace(str(staged) + '/', '')
                match = re.search(r'([^\n:]+\.proto):(\d+)(?::\d+)?:', diagnostic)
                raise ConversionError(f'协议校验或 {language_label} 生成失败，请检查错误详情。', 'COMPILATION_FAILED',
                                      file=match[1] if match else None, line=int(match[2]) if match else None,
                                      details=diagnostic) from exc
            compiler_warnings = generated.stderr.strip().replace(str(staged) + '/', '')
            if compiler_warnings:
                warnings.append('编译器警告：\n' + compiler_warnings)
            java_count = len(list((staged / 'java').rglob('*.java')))
            objc_count = sum(1 for path in (staged / 'objc').rglob('*')
                             if path.is_file() and path.name.endswith(('.pbobjc.h', '.pbobjc.m')))
            result = ConversionResult(None, len(logical), java_count, version, request.target_runtime,
                                      warnings, changes, excluded, source_files,
                                      output_language=request.output_language, objc_count=objc_count,
                                      generated_count=java_count + objc_count)
        progress('exporting', '正在整理输出文件' if request.action == 'inspect' else '正在发布输出文件',
                 processed=result.proto_count, total=result.proto_count)
        if request.action == 'inspect' and preparation_target is not None:
            prepared.save(staged, snapshot, result, token)
        if tracking and not _unchanged(snapshot, request, inputs, protoc, version, token, (Path(temp),)):
            raise ConversionError('转换过程中输入、依赖或编译器发生变化，已取消发布。', 'INPUT_CHANGED', '请等待文件保存完成后重新检查并转换。')
        if request.action == 'convert':
            with token.publication():
                _check_destination(destination)
                if destination.exists():
                    destination.rmdir()
                os.rename(staged, destination)
                result.output_path = str(destination)
        elif preparation_target is not None:
            with token.publication():
                if preparation_target.exists() or preparation_target.is_symlink():
                    raise ConversionError('预检成果目标已被占用，未覆盖已有目录。', 'INVALID_REQUEST')
                os.rename(staged, preparation_target)
                result.preparation_path = str(preparation_target)
        token.check()
        return result
