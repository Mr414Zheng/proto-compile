#!/usr/bin/env python3
"""在临时源码副本中展开实际页面，运行本机架构的原生工作区测试。"""
import os
import platform
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / 'macos/Sources'
with tempfile.TemporaryDirectory(prefix='proto-workspace-tests-') as directory:
    folder = Path(directory)
    files = []
    for name in ['AppModel.swift', 'BleLogModel.swift', 'EngineClient.swift', 'WorkspaceView.swift', 'ContentView.swift', 'BleLogView.swift']:
        source = (SOURCES / name).read_text()
        if name == 'ContentView.swift':
            original = '@StateObject private var options = ViewOptions()'
            replacement = '@StateObject private var options: ViewOptions = { let value = ViewOptions(); value.changes = true; value.errorDetails = true; return value }()'
            assert source.count(original) == 1
            source = source.replace(original, replacement)
        elif name == 'BleLogView.swift':
            original = '@StateObject private var options = BleViewOptions()'
            replacement = '@StateObject private var options: BleViewOptions = { let value = BleViewOptions(); value.warningsExpanded = true; value.errorDetails = true; return value }()'
            assert source.count(original) == 1
            source = source.replace(original, replacement)
        target = folder / name
        target.write_text(source)
        files.append(str(target))
    executable = folder / 'WorkspaceViewTests'
    subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-swift-version', '5', '-O',
                    '-module-cache-path', '/tmp/proto-swift-cache', '-target', f'{platform.machine()}-apple-macosx13.0',
                    '-framework', 'SwiftUI', '-framework', 'AppKit', *files,
                    str(ROOT / 'macos/Tests/WorkspaceViewTests.swift'), '-o', str(executable)], check=True)
    environment = dict(os.environ, PROTO_COMPILE_UI_TEST_EXPANDED_FIXTURE='1')
    subprocess.run([str(executable)], env=environment, check=True)
