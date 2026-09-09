import hashlib
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import ProtoError
from .extract import safe_name


ROOT = Path(__file__).resolve().parent.parent
VERSION = '3.11.0'
RUNTIME_VERSION = '3.11.0'
INSTALL = ROOT / '.tools' / ('protoc-' + VERSION)
# 3.11.0 没有 macOS ARM 包；Apple Silicon 通过 Rosetta 运行同一 Intel 包。
# 此旧版发布页没有提供摘要，以下 SHA-256 固定自官方 HTTPS 下载的安装包。
ASSETS = {
    'arm64': ('osx-x86_64', 'd35dfbb430cd242eb0405557c7e189c68abb1d85b11674bf8b302ed6c6360646'),
    'x86_64': ('osx-x86_64', 'd35dfbb430cd242eb0405557c7e189c68abb1d85b11674bf8b302ed6c6360646'),
}


def run(command, cwd=None):
    try:
        result = subprocess.run([str(x) for x in command], cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise ProtoError(f'无法运行 {command[0]}: {exc}') from exc
    if result.returncode:
        raise ProtoError(f'命令失败 ({result.returncode}): {command[0]}\n{result.stderr.strip() or result.stdout.strip()}')
    return result


def verify_archive(archive, expected):
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected:
        raise ProtoError(f'protoc 下载校验失败: 期望 {expected}，实际 {digest}')


def unpack(archive, directory):
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            safe_name(info.filename.rstrip('/'))
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ProtoError(f'压缩包不允许符号链接: {info.filename}')
        bundle.extractall(directory)


def setup():
    if platform.system() != 'Darwin' or platform.machine() not in ASSETS:
        raise ProtoError('自动安装仅支持 macOS arm64/x86_64；其他平台请安装 protoc 并通过 --protoc 指定')
    executable = INSTALL / 'bin' / 'protoc'
    if INSTALL.exists():
        if executable.is_file() and run([executable, '--version']).stdout.strip() == 'libprotoc ' + VERSION:
            return executable
        raise ProtoError(f'已有安装不完整或版本不符，请移走后重试: {INSTALL}')
    INSTALL.parent.mkdir(parents=True, exist_ok=True)
    asset, checksum = ASSETS[platform.machine()]
    url = f'https://github.com/protocolbuffers/protobuf/releases/download/v{VERSION}/protoc-{VERSION}-{asset}.zip'
    with tempfile.TemporaryDirectory(prefix='protoc-setup-', dir=INSTALL.parent) as temp:
        temp = Path(temp)
        archive = temp / 'protoc.zip'
        try:
            with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as target:
                shutil.copyfileobj(response, target)
        except OSError as exc:
            raise ProtoError(f'下载 protoc 失败: {exc}') from exc
        verify_archive(archive, checksum)
        contents = temp / 'contents'
        unpack(archive, contents)
        binary = contents / 'bin' / 'protoc'
        binary.chmod(binary.stat().st_mode | 0o111)
        if run([binary, '--version']).stdout.strip() != 'libprotoc ' + VERSION:
            raise ProtoError('下载的 protoc 版本不符')
        os.rename(contents, INSTALL)
    return executable


def find_protoc(explicit=None):
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            found = shutil.which(explicit)
            if not found:
                raise ProtoError(f'找不到指定的 protoc: {explicit}')
            candidate = Path(found)
    elif (INSTALL / 'bin' / 'protoc').is_file():
        candidate = INSTALL / 'bin' / 'protoc'
    else:
        found = shutil.which('protoc')
        if not found:
            raise ProtoError('未找到 protoc；请先运行 python3 proto_compile.py setup，或使用 --protoc 指定路径')
        candidate = Path(found)
    candidate = candidate.resolve()
    version = run([candidate, '--version']).stdout.strip()
    return candidate, version
