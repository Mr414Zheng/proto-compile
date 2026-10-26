"""预检成果的完整性校验与只读复制；不清理调用者管理的目录。"""

import hashlib
import json
import os
import shutil
from pathlib import Path

from . import ProtoError
from .extract import safe_name

FORMAT_VERSION = 1
MANIFEST_NAME = 'manifest.json'
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


class InvalidPreparation(ValueError):
    pass


def _manifest_digest(data):
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def file_hash(path, cancellation=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(256 * 1024):
            if cancellation:
                cancellation.check()
            digest.update(block)
    if cancellation:
        cancellation.check()
    return digest.hexdigest()


def fingerprints(paths, cancellation=None):
    return {str(path): file_hash(path, cancellation) for path in sorted(set(map(Path, paths)))}


def identity(request, protoc, version):
    """仅记录影响校验和代码生成的参数，导出位置不会使预检成果失效。"""
    return dict(input_kind=request.input_kind,
                input_paths=[str(Path(p).expanduser().resolve()) for p in request.input_paths],
                include_paths=[str(Path(p).expanduser().resolve()) for p in request.include_paths],
                target_runtime=request.target_runtime, compiler_path=str(protoc), compiler_version=version,
                exclude_opt_0081=request.exclude_opt_0081, java_package=request.java_package,
                no_repairs=request.no_repairs,
                repairs=str(Path(request.repairs).expanduser().resolve()) if request.repairs else None)


def directory_entries(inputs, ignored=()):
    return {str(path): [p.relative_to(path).as_posix() for p in sorted(path.rglob('*.proto'))
                       if not any(p.is_relative_to(root) for root in ignored) and p.is_file()]
            for path in inputs if path.is_dir()}


def binding_state(names, roots):
    # 只检查已经使用的逻辑名，不遍历 include 根中无关的成千上万份文件。
    return {name: list(dict.fromkeys(str((root / name).resolve()) for root in roots if (root / name).is_file()))
            for name in sorted(names)}


def artifact_hashes(root, cancellation=None):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise InvalidPreparation('预检目录不存在或为符号链接')
    if not all((root / name).is_dir() and not (root / name).is_symlink() for name in ('java', 'proto')):
        raise InvalidPreparation('预检目录结构无效')
    result = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current = Path(current)
        if cancellation:
            cancellation.check()
        for name in directories:
            path = current / name
            if path.is_symlink() or path.relative_to(root).parts[0] not in ('java', 'proto'):
                raise InvalidPreparation('预检目录包含非法目录或符号链接')
        for name in files:
            path = current / name
            relative = path.relative_to(root)
            if path.is_symlink():
                raise InvalidPreparation('预检目录包含符号链接')
            if relative.as_posix() == MANIFEST_NAME:
                continue
            if relative.parts[0] not in ('java', 'proto') or path.suffix != ('.java' if relative.parts[0] == 'java' else '.proto'):
                raise InvalidPreparation('预检目录包含非转换产物')
            result[relative.as_posix()] = file_hash(path, cancellation)
    return result


def load(path, cancellation=None, expected_snapshot=None):
    root = Path(path)
    manifest = root / MANIFEST_NAME
    if root.is_symlink() or manifest.is_symlink():
        raise InvalidPreparation('预检目录或清单为符号链接')
    with manifest.open('rb') as source:
        raw = source.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise InvalidPreparation('预检清单过大')
    data = json.loads(raw)
    if not isinstance(data, dict) or type(data.get('format_version')) is not int or data['format_version'] != FORMAT_VERSION:
        raise InvalidPreparation('预检清单版本无效')
    checksum = data.pop('integrity', None)
    if checksum != _manifest_digest(data):
        raise InvalidPreparation('预检清单校验失败')
    artifacts = data.get('artifacts')
    result = data.get('result')
    if not isinstance(data.get('snapshot'), dict) or not isinstance(artifacts, dict) or not isinstance(result, dict):
        raise InvalidPreparation('预检清单结构无效')
    if expected_snapshot is not None and data['snapshot'] != expected_snapshot:
        raise InvalidPreparation('输入或配置已变化')
    for name, digest in artifacts.items():
        try:
            safe_name(name)
        except ProtoError as exc:
            raise InvalidPreparation('预检产物路径无效') from exc
        if not isinstance(digest, str) or len(digest) != 64:
            raise InvalidPreparation('预检产物摘要无效')
    for key in ('proto_count', 'java_count'):
        if type(result.get(key)) is not int or result[key] < 0:
            raise InvalidPreparation('预检计数无效')
    for key in ('warnings', 'changes', 'excluded', 'source_files'):
        if not isinstance(result.get(key), list) or not all(isinstance(value, str) for value in result[key]):
            raise InvalidPreparation('预检结果结构无效')
    if result['proto_count'] != sum(name.startswith('proto/') for name in artifacts) or result['java_count'] != sum(name.startswith('java/') for name in artifacts):
        raise InvalidPreparation('预检产物计数不一致')
    if artifact_hashes(root, cancellation) != artifacts:
        raise InvalidPreparation('预检产物校验失败')
    return data


def save(root, snapshot, result, cancellation=None):
    artifacts = artifact_hashes(root, cancellation)
    data = dict(format_version=FORMAT_VERSION, snapshot=snapshot, artifacts=artifacts,
                result={key: getattr(result, key) for key in ('proto_count', 'java_count', 'compiler_version',
                        'runtime_version', 'warnings', 'changes', 'excluded', 'source_files')})
    data['integrity'] = _manifest_digest(data)
    (Path(root) / MANIFEST_NAME).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def copy_verified(path, destination, manifest, cancellation=None):
    for name in ('java', 'proto'):
        (destination / name).mkdir(parents=True, exist_ok=True)
    for name in manifest['artifacts']:
        if cancellation:
            cancellation.check()
        source = Path(path)
        for part in Path(name).parts:
            source = source / part
            if source.is_symlink():
                raise InvalidPreparation('复制时预检目录发生变化')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if artifact_hashes(destination, cancellation) != manifest['artifacts']:
        raise InvalidPreparation('复制后的预检产物校验失败')
