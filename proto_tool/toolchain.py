import hashlib
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import ProtoError
from .extract import safe_name


ROOT = Path(__file__).resolve().parent.parent
VERSION = '25.9'
RUNTIME_VERSION = '3.25.9'
COMPILER_VERSIONS = {'3.25.9': '25.9', '3.6.1': '3.6.1'}
TARGET_RUNTIMES = tuple(COMPILER_VERSIONS)
INSTALL = ROOT / '.tools' / 'native-protoc' / RUNTIME_VERSION / platform.machine()


def validate_target_runtime(target_runtime):
    if target_runtime not in TARGET_RUNTIMES:
        raise ProtoError(f'不支持的 Java 运行库版本: {target_runtime!r}；请选择 3.25.9 或 3.6.1')
    return target_runtime


def native_protoc(target_runtime):
    return ROOT / '.tools' / 'native-protoc' / target_runtime / platform.machine() / 'bin' / 'protoc'


def run(command, cwd=None, cancellation=None):
    try:
        if cancellation is None:
            result = subprocess.run([str(x) for x in command], cwd=cwd, capture_output=True, text=True)
        else:
            result = cancellation.run([str(x) for x in command], cwd=cwd)
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
    import zipfile

    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            safe_name(info.filename.rstrip('/'))
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ProtoError(f'压缩包不允许符号链接: {info.filename}')
        bundle.extractall(directory)


def setup(target_runtime=RUNTIME_VERSION):
    """通过项目 bootstrap 准备指定运行库对应的原生工具链。"""
    validate_target_runtime(target_runtime)
    if getattr(sys, 'frozen', False):
        return find_protoc(target_runtime=target_runtime)[0]
    if platform.system() != 'Darwin' or platform.machine() not in ('arm64', 'x86_64'):
        raise ProtoError('自动准备工具链仅支持 macOS arm64/x86_64；其他平台请通过 --protoc 指定匹配版本的编译器')
    native = native_protoc(target_runtime)
    if not native.is_file():
        bootstrap = ROOT / 'scripts' / 'bootstrap.py'
        if not bootstrap.is_file():
            raise ProtoError('找不到工具链准备脚本 scripts/bootstrap.py，请重新获取完整项目')
        run([sys.executable, bootstrap, '--target-runtime', target_runtime, '--arch', platform.machine()])
    return find_protoc(native, target_runtime=target_runtime)[0]


def find_protoc(explicit=None, cancellation=None, target_runtime=RUNTIME_VERSION):
    validate_target_runtime(target_runtime)
    native = native_protoc(target_runtime)
    if getattr(sys, 'frozen', False):
        # 冻结应用必须自包含，不能误用用户 PATH 中版本不兼容的编译器。
        candidate = Path(sys._MEIPASS) / 'toolchains' / target_runtime / 'bin' / 'protoc'
        if not candidate.is_file():
            raise ProtoError(f'应用内置 protoc 缺失（Java 运行库 {target_runtime}），请重新安装应用')
    elif explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            found = shutil.which(str(explicit))
            if not found:
                raise ProtoError(f'找不到指定的 protoc: {explicit}')
            candidate = Path(found)
    elif native.is_file():
        candidate = native
    else:
        found = shutil.which('protoc')
        if not found:
            raise ProtoError(f'未找到 protoc；请先运行 python3 proto_compile.py setup --target-runtime {target_runtime}，或使用 --protoc 指定匹配编译器')
        candidate = Path(found)
    candidate = candidate.resolve()
    version = run([candidate, '--version'], cancellation=cancellation).stdout.strip()
    expected = 'libprotoc ' + COMPILER_VERSIONS[target_runtime]
    if version != expected:
        raise ProtoError(f'编译器与 Java 运行库版本不匹配：目标 protobuf-java {target_runtime} 需要 {expected}，实际为 {version or "未知版本"}。请准备对应工具链或调整 --target-runtime')
    return candidate, version
