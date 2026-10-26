#!/usr/bin/env python3
"""在项目 .tools 内准备构建环境和双架构原生 protoc，不安装系统组件。"""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.tools'
BUILD = CACHE / 'build'
FRAMEWORK = BUILD / 'Python.framework'
PYTHON_HOME = FRAMEWORK / 'Versions/3.12'
PYTHON = PYTHON_HOME / 'bin/python3.12'
RUNTIMES = {'3.25.9': '25.9', '3.6.1': '3.6.1'}
PROTO_SOURCES = {runtime: BUILD / ('sources/protobuf-' + compiler) for runtime, compiler in RUNTIMES.items()}
DOWNLOADS = {
    'python-3.12.10-macos11.pkg': (
        'https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg',
        '8373e58da4ea146b3eb1c1f9834f19a319440b6b679b06050b1f9ee3237aa8e4'),
    'protobuf-cpp-3.6.1.tar.gz': (
        'https://github.com/protocolbuffers/protobuf/releases/download/v3.6.1/protobuf-cpp-3.6.1.tar.gz',
        'b3732e471a9bb7950f090fd0457ebd2536a9ba0891b7f3785919c654fe2a2529'),
    'protobuf-25.9.tar.gz': (
        'https://github.com/protocolbuffers/protobuf/releases/download/v25.9/protobuf-25.9.tar.gz',
        '272989ca30fc9781e6dd35f5e9481feb79b0c5861c208667aa2fa5503ee0eda0'),
    'protoc-25.9-osx-aarch_64.zip': (
        'https://github.com/protocolbuffers/protobuf/releases/download/v25.9/protoc-25.9-osx-aarch_64.zip',
        '11e91bb16fc52f5691970a3fb57c6eb26e13065a2eff1eea226b0dcd698ddb6d'),
    'protoc-25.9-osx-x86_64.zip': (
        'https://github.com/protocolbuffers/protobuf/releases/download/v25.9/protoc-25.9-osx-x86_64.zip',
        '387ad924f98fee962285ff3c5d63e1c6c666ea76ba339fb506afa56d74372c60'),
}


def run(args, **kwargs):
    print('+', ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def build_env():
    # 提取的官方 Framework 保留原安装路径，通过构建进程环境定位；产物不使用这些变量。
    env = dict(os.environ)
    env.update(DYLD_FRAMEWORK_PATH=str(BUILD), DYLD_LIBRARY_PATH=str(PYTHON_HOME / 'lib'),
               PYTHONHOME=str(PYTHON_HOME), SSL_CERT_FILE='/etc/ssl/cert.pem',
               PIP_CERT='/etc/ssl/cert.pem', PYTHONDONTWRITEBYTECODE='1',
               PYTHONPYCACHEPREFIX=str(BUILD / 'pycache'))
    return env


def fetch(name):
    url, expected = DOWNLOADS[name]
    destination = CACHE / 'downloads' / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        partial = destination.with_suffix(destination.suffix + '.partial')
        try:
            with urllib.request.urlopen(url, timeout=120) as response, partial.open('wb') as out:
                shutil.copyfileobj(response, out)
            if hashlib.sha256(partial.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f'下载校验失败: {name}')
            partial.rename(destination)
        finally:
            partial.unlink(missing_ok=True)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f'缓存校验失败，请移走后重试: {destination}')
    return destination


def prepare_runtime():
    package = fetch('python-3.12.10-macos11.pkg')
    if not PYTHON.exists():
        extraction = BUILD / 'python-pkg'
        extraction.mkdir(parents=True, exist_ok=True)
        FRAMEWORK.mkdir(parents=True, exist_ok=True)
        run(['/usr/bin/xar', '-xf', package, '-C', extraction])
        run(['/usr/bin/tar', '-xf', extraction / 'Python_Framework.pkg/Payload', '-C', FRAMEWORK])
    relocate_framework()
    run([PYTHON, '-m', 'ensurepip', '--upgrade'], env=build_env())
    run([PYTHON, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-warn-script-location',
         '-r', ROOT / 'scripts/build-requirements.txt'], env=build_env())


def relocate_framework():
    # PyInstaller 的架构包装器经过 /usr/bin/arch，会清除 DYLD 环境变量。
    # 只改项目缓存中的构建 Python，让隔离子进程也能找到同一份 Framework。
    original = '/Library/Frameworks/Python.framework/'
    magic = {bytes.fromhex(value) for value in ['feedface', 'cefaedfe', 'feedfacf', 'cffaedfe', 'cafebabe', 'bebafeca']}
    for path in FRAMEWORK.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) not in magic:
                continue
        links = subprocess.check_output(['/usr/bin/otool', '-L', str(path)], text=True)
        changes = []
        for line in links.splitlines():
            link = line.strip().split(' (', 1)[0]
            if link.startswith(original):
                changes += ['-change', link, str(FRAMEWORK / link[len(original):])]
        if changes:
            run(['/usr/bin/install_name_tool', *changes, path])
            run(['/usr/bin/codesign', '--force', '--sign', '-', path])


def build_protoc(arch, jobs, runtime):
    source = PROTO_SOURCES[runtime]
    archive = fetch('protobuf-cpp-3.6.1.tar.gz' if runtime == '3.6.1' else 'protobuf-25.9.tar.gz')
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        # 固定摘要的官方源码包，只解压到项目构建缓存。
        with tarfile.open(archive) as bundle:
            bundle.extractall(source.parent)
    native = CACHE / 'native-protoc' / runtime / arch
    if runtime == '3.25.9':
        # 新版有官方原生双架构发布包，旧版则必须从源码构建，避免依赖 Rosetta。
        asset_arch = 'aarch_64' if arch == 'arm64' else arch
        archive = fetch(f'protoc-25.9-osx-{asset_arch}.zip')
        native.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(native)
        (native / 'bin/protoc').chmod(0o755)
        verify_protoc(native, arch, runtime)
        return
    cmake = PYTHON_HOME / 'lib/python3.12/site-packages/cmake/data/bin/cmake'
    output = BUILD / ('protoc-' + runtime + '-' + arch)
    sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
    run([cmake, '-S', source / 'cmake', '-B', output,
         '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_STANDARD=14',
         '-Dprotobuf_BUILD_TESTS=OFF', '-Dprotobuf_BUILD_SHARED_LIBS=OFF', '-Dprotobuf_WITH_ZLIB=OFF',
         '-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0', '-DCMAKE_OSX_ARCHITECTURES=' + arch,
         '-DCMAKE_OSX_SYSROOT=' + sdk, '-DCMAKE_INSTALL_PREFIX=' + str(native)])
    run([cmake, '--build', output, '--parallel', str(jobs), '--target', 'install'])
    verify_protoc(native, arch, runtime)


def verify_protoc(native, arch, runtime):
    run(['/usr/bin/lipo', '-verify_arch', arch, native / 'bin/protoc'])
    if platform.machine() == arch:
        version = subprocess.check_output([str(native / 'bin/protoc'), '--version'], text=True).strip()
        if version != 'libprotoc ' + RUNTIMES[runtime]:
            raise RuntimeError(f'编译器版本不符: {version}')
    print(f'原生 protoc 已生成: {native}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', nargs='+', choices=['arm64', 'x86_64'], default=['arm64', 'x86_64'])
    parser.add_argument('--target-runtime', nargs='+', choices=list(RUNTIMES), default=list(RUNTIMES))
    parser.add_argument('--jobs', type=int, default=min(8, os.cpu_count() or 2))
    parser.add_argument('--skip-runtime', action='store_true', help='已准备构建 Python 时跳过 pip 安装')
    args = parser.parse_args()
    if platform.system() != 'Darwin':
        parser.error('构建需要 macOS 和 Xcode')
    if args.jobs < 1:
        parser.error('--jobs 必须大于 0')
    BUILD.mkdir(parents=True, exist_ok=True)
    if not args.skip_runtime:
        prepare_runtime()
    for runtime in dict.fromkeys(args.target_runtime):
        for arch in dict.fromkeys(args.arch):
            build_protoc(arch, args.jobs, runtime)


if __name__ == '__main__':
    main()
