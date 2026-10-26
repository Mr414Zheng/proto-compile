#!/usr/bin/env python3
"""Build equal-overhead GUI benchmark copies without changing the baseline snapshot."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
HOOKS = [
    ('    private var quitting = false', '    private var quitting = false\n    private var performanceDriver: PerformanceDriver?'),
    ('        let window = makeWindow()\n        self.window = window\n        window.makeKeyAndOrderFront(nil)', '''        if args.contains("--performance-test") {
            do { performanceDriver = try PerformanceDriver(model: model, arguments: args) }
            catch { fputs("Performance setup failed: \\(error)\\n", stderr); exit(2) }
        }
        let window = makeWindow()
        self.window = window
        if performanceDriver != nil { window.setContentSize(NSSize(width: 820, height: 800)); window.center() }
        window.makeKeyAndOrderFront(nil)'''),
    ('        model.loadVersion()\n    }\n\n    private func makeWindow()', '        model.loadVersion()\n        performanceDriver?.start()\n    }\n\n    private func makeWindow()'),
    ('    @MainActor static func main() {\n        let delegate', '    @MainActor static func main() {\n        PerformanceDriver.markLaunchIfRequested()\n        let delegate'),
]

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, default=ROOT / '.tools/performance/baseline-20261026-152822')
    parser.add_argument('--current-app', type=Path, default=ROOT / 'dist/arm64/Proto Compile.app')
    parser.add_argument('--output-root', type=Path, default=ROOT / '.tools/performance')
    args = parser.parse_args()
    driver = ROOT / 'macos/Sources/PerformanceDriver.swift'
    original_manifest = args.baseline / 'manifest.json'
    manifest_hash = hashlib.sha256(original_manifest.read_bytes()).hexdigest()
    for label, source_root, original_app in [
        ('baseline', args.baseline / 'macos', args.baseline / 'Proto Compile.app'),
        ('current', ROOT / 'macos', args.current_app),
    ]:
        folder = args.output_root / f'instrumented-{label}'
        if folder.exists():
            raise SystemExit(f'Refusing to overwrite existing fixture: {folder}')
        source = folder / 'macos'
        app = folder / 'Proto Compile.app'
        folder.mkdir(parents=True)
        shutil.copytree(source_root, source, symlinks=True)
        shutil.copytree(original_app, app, symlinks=True)
        entry = source / 'Sources/ProtoCompileApp.swift'
        if label == 'baseline':
            text = entry.read_text()
            for old, new in HOOKS:
                if text.count(old) != 1:
                    raise SystemExit(f'Baseline entry hook changed: {old!r}')
                text = text.replace(old, new, 1)
            entry.write_text(text)
            model_file = source / 'Sources/AppModel.swift'
            model_text = model_file.read_text()
            model_text = model_text.replace('    private func present(_ panel:', '    func setOutputParent(_ path: String) { outputParent = path; parametersChanged() }\n\n    private func present(_ panel:', 1)
            model_file.write_text(model_text)
        shutil.copy2(driver, source / 'Sources/PerformanceDriver.swift')
        binary = app / 'Contents/MacOS/ProtoCompile'
        command = ['xcrun', 'swiftc', '-parse-as-library', '-swift-version', '5', '-O',
                   '-sdk', subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip(),
                   '-module-cache-path', '/tmp/proto-swift-module-cache',
                   '-target', 'arm64-apple-macosx13.0', '-framework', 'SwiftUI', '-framework', 'AppKit',
                   *map(str, sorted((source / 'Sources').glob('*.swift'))), '-o', str(binary)]
        subprocess.run(command, check=True, cwd=ROOT)
        subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
        (folder / 'instrumentation.json').write_text(json.dumps({
            'baseline_manifest_sha256': manifest_hash,
            'driver_sha256': hashlib.sha256(driver.read_bytes()).hexdigest(),
            'binary': str(binary), 'compile_command': command,
            'source_origin': str(source_root), 'bundle_origin': str(original_app),
            'scenario': ['launch', 'idle:2s', 'inspect_input', 'inspect_output', 'convert', 'after:2s', 'shutdown'],
            'driver_interval_seconds': 0.04,
        }, ensure_ascii=False, indent=2) + '\n')
        print(binary, flush=True)
    if hashlib.sha256(original_manifest.read_bytes()).hexdigest() != manifest_hash:
        raise SystemExit('Baseline manifest changed unexpectedly')

if __name__ == '__main__':
    main()
