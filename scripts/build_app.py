#!/usr/bin/env python3
"""构建自包含、按架构分发的 Proto Compile.app 和 ZIP。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

from bootstrap import ROOT, BUILD, PYTHON, PYTHON_HOME, PROTO_SOURCES, RUNTIMES, build_env, run

MACHO = {bytes.fromhex(value) for value in ['feedface', 'cefaedfe', 'feedfacf', 'cffaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca']}


def sign_contents(app):
    binaries = []
    for path in app.rglob('*'):
        if path.is_file() and not path.is_symlink():
            if path.parent.name == 'MacOS':
                continue
            with path.open('rb') as stream:
                if stream.read(4) in MACHO:
                    binaries.append(path)
    for path in sorted(binaries, key=lambda p: len(p.parts), reverse=True):
        run(['/usr/bin/codesign', '--force', '--sign', '-', path])
    frameworks = [path for path in app.rglob('*.framework') if path.is_dir() and not path.is_symlink()]
    for framework in sorted(frameworks, key=lambda p: len(p.parts), reverse=True):
        run(['/usr/bin/codesign', '--force', '--sign', '-', framework])
    for nested in sorted(app.rglob('*.app'), key=lambda p: len(p.parts), reverse=True):
        run(['/usr/bin/codesign', '--force', '--sign', '-', nested])
    run(['/usr/bin/codesign', '--force', '--sign', '-', app])
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app])


def licenses(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for runtime, source in PROTO_SOURCES.items():
        shutil.copy2(source / 'LICENSE', directory / f'protobuf-{RUNTIMES[runtime]}.txt')
        for notice in (source / 'third_party').rglob('LICENSE*'):
            if notice.is_file():
                name = '-'.join(notice.relative_to(source).parts)
                shutil.copy2(notice, directory / f'protobuf-{RUNTIMES[runtime]}-{name}')
    shutil.copy2(PYTHON_HOME / 'lib/python3.12/LICENSE.txt', directory / 'Python-3.12.10.txt')
    for notice in (ROOT / 'scripts/licenses').iterdir():
        if notice.is_file():
            shutil.copy2(notice, directory / notice.name)
    site = PYTHON_HOME / 'lib/python3.12/site-packages'
    for pattern, output in [('pyinstaller-*.dist-info/licenses/COPYING.txt', 'PyInstaller.txt'),
                            ('altgraph-*.dist-info/LICENSE', 'altgraph.txt')]:
        found = list(site.glob(pattern))
        if found:
            shutil.copy2(found[0], directory / output)
    (directory / 'README.txt').write_text(
        'Proto Compile 通过 PyInstaller 内置 CPython 3.12.10、protoc 25.9 和 protoc 3.6.1。\n'
        'Python: https://www.python.org/\nProtobuf: https://github.com/protocolbuffers/protobuf\n'
        'PyInstaller: https://pyinstaller.org/（GPL 许可附带分发例外）\n', encoding='utf-8')


def icon(resources, sdk):
    executable = BUILD / 'make-icon'
    run(['xcrun', 'swiftc', '-module-cache-path', BUILD / 'swift-cache', '-sdk', sdk,
         ROOT / 'scripts/make_icon.swift', '-o', executable])
    folder = BUILD / 'ProtoCompile.iconset'
    run([executable, folder])
    run(['/usr/bin/iconutil', '-c', 'icns', folder, '-o', resources / 'AppIcon.icns'])


def build(arch, destination):
    compilers = {runtime: ROOT / '.tools/native-protoc' / runtime / arch / 'bin/protoc' for runtime in RUNTIMES}
    if not PYTHON.exists() or not all(path.exists() for path in compilers.values()):
        raise RuntimeError('构建环境未就绪，请先运行 python3 scripts/bootstrap.py')
    toolchain_arguments = []
    for runtime, protoc in compilers.items():
        toolchain_arguments += ['--add-data', str(protoc.parent.parent / 'include') + f':toolchains/{runtime}/include',
                                '--add-binary', str(protoc) + f':toolchains/{runtime}/bin']
    sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
    work = BUILD / ('app-' + arch)
    work.mkdir(parents=True, exist_ok=True)
    env = build_env()
    env['PYINSTALLER_CONFIG_DIR'] = str(BUILD / 'pyinstaller-cache')
    env['MACOSX_DEPLOYMENT_TARGET'] = '13.0'
    run([PYTHON, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed', '--optimize', '2', '--name', 'ConversionEngine',
         '--osx-bundle-identifier', 'com.protocompile.engine',
         '--target-arch', arch, '--contents-directory', '_internal',
         '--distpath', work / 'engine-dist', '--workpath', work / 'engine-work', '--specpath', work,
         '--add-data', str(ROOT / 'config') + ':config',
         *toolchain_arguments,
         ROOT / 'engine_main.py'], env=env, cwd=ROOT)
    engine = work / 'engine-dist/ConversionEngine.app'
    engine_info = plistlib.loads((engine / 'Contents/Info.plist').read_bytes())
    engine_info['LSBackgroundOnly'] = True
    engine_info['LSUIElement'] = True
    (engine / 'Contents/Info.plist').write_bytes(plistlib.dumps(engine_info))
    destination = destination / arch
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.app-package-', dir=destination) as temporary:
        app = Path(temporary) / 'Proto Compile.app'
        contents = app / 'Contents'
        (contents / 'MacOS').mkdir(parents=True)
        (contents / 'Resources').mkdir()
        (contents / 'Helpers').mkdir()
        info = plistlib.loads((ROOT / 'macos/Info.plist').read_bytes())
        info['CFBundleIconFile'] = 'AppIcon'
        info['LSArchitecturePriority'] = [arch]
        (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
        (contents / 'PkgInfo').write_bytes(b'APPL????')
        sources = sorted((ROOT / 'macos/Sources').glob('*.swift'))
        run(['xcrun', 'swiftc', '-parse-as-library', '-swift-version', '5', '-O',
             '-module-cache-path', BUILD / 'swift-cache', '-target', f'{arch}-apple-macosx13.0',
             '-sdk', sdk, '-framework', 'SwiftUI', '-framework', 'AppKit', *sources,
             '-o', contents / 'MacOS/ProtoCompile'])
        shutil.copytree(engine, contents / 'Helpers/ConversionEngine.app', symlinks=True)
        licenses(contents / 'Resources/Licenses')
        icon(contents / 'Resources', sdk)
        sign_contents(app)
        binaries = [contents / 'MacOS/ProtoCompile', contents / 'Helpers/ConversionEngine.app/Contents/MacOS/ConversionEngine']
        binaries += [contents / f'Helpers/ConversionEngine.app/Contents/Frameworks/toolchains/{runtime}/bin/protoc'
                     for runtime in RUNTIMES]
        for binary in binaries:
            run(['/usr/bin/lipo', '-verify_arch', arch, binary])
        final = destination / app.name
        if final.exists():
            shutil.rmtree(final)
        shutil.move(str(app), final)
    archive = destination.parent / f'Proto-Compile-{info["CFBundleShortVersionString"]}-{arch}.zip'
    archive.unlink(missing_ok=True)
    run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', final, archive])
    print(f'完成: {final}\n压缩包: {archive}\nSHA-256: {hashlib.sha256(archive.read_bytes()).hexdigest()}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', nargs='+', choices=['arm64', 'x86_64'], default=['arm64', 'x86_64'])
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    for arch in dict.fromkeys(args.arch):
        build(arch, args.output.resolve())


if __name__ == '__main__':
    main()
