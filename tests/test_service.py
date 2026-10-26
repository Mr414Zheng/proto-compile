import html
from contextlib import redirect_stdout
from dataclasses import asdict, replace
import hashlib
import uuid
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from proto_tool.engine import main, parse_request
from proto_tool.cli import main as cli_main
from proto_tool import ProtoError, prepared, service
from proto_tool.extract import Source
from proto_tool.repairs import repair_sources
from proto_tool.service import (CancellationToken, Cancelled, ConversionError,
                                ConversionRequest, collect_proto_files, convert)
from proto_tool.toolchain import COMPILER_VERSIONS, RUNTIME_VERSION, find_protoc, setup

ROOT = Path(__file__).resolve().parent.parent


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='协议 service ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compiler = self.root / 'toolchains/3.25.9/bin/protoc'
        self.compiler.parent.mkdir(parents=True)
        self.standard = self.root / 'toolchains/3.25.9/include'
        self.standard.mkdir()
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.output = self.root / 'outputs'
        self.output.mkdir()

    def proto(self, name, content='syntax="proto2"; message A {}', root=None):
        path = (root or self.inputs) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def fake_compiler(self, sleep=False, fail=False, runtime=RUNTIME_VERSION):
        self.compiler.write_text('#!' + sys.executable + '\n' + '''import pathlib, sys, time
if '--version' in sys.argv:
    print('libprotoc COMPILER_VERSION')
    raise SystemExit(0)
''' + ('time.sleep(30)\n' if sleep else '') + ('''print('main.proto:2:3: invalid field', file=sys.stderr)
raise SystemExit(1)
''' if fail else '''out = pathlib.Path(next(s.split('=', 1)[1] for s in sys.argv if s.startswith('--java_out=')))
for item in sys.argv[1:]:
    if item.endswith('.proto'):
        (out / (pathlib.Path(item).stem + '.java')).write_text('// generated fixture')
'''))
        self.compiler.write_text(self.compiler.read_text().replace('COMPILER_VERSION', COMPILER_VERSIONS[runtime]))
        self.compiler.chmod(0o755)

    def request(self, files=None, **kwargs):
        return ConversionRequest('proto', files or [self.proto('main.proto')], output_parent=self.output,
                                 protoc=self.compiler, **kwargs)

    def test_recursive_dependency_and_standard_include_only(self):
        main = self.proto('main.proto', 'syntax="proto3"; import "sub/a.proto"; import "google/protobuf/empty.proto"; message Main {}')
        self.proto('sub/a.proto', 'syntax="proto3"; import public "b.proto"; message A {}')
        self.proto('b.proto', 'syntax="proto3"; message B {}')
        self.proto('google/protobuf/empty.proto', 'syntax="proto3"; message Empty {}', self.standard)
        logical, _ = collect_proto_files([main], [self.inputs], self.compiler)
        self.assertEqual(set(logical), {'main.proto', 'sub/a.proto', 'b.proto'})
        self.fake_compiler()
        result = convert(self.request([main]))
        self.assertEqual((result.proto_count, result.java_count), (3, 3))
        output = Path(result.output_path)
        self.assertTrue(output.name.startswith('协议转换-'))
        self.assertEqual({p.name for p in output.iterdir()}, {'java', 'proto'})
        self.assertFalse((output / 'proto/google').exists())
        self.assertEqual(len(result.source_files), 3)

    def test_missing_and_shadowed_dependency(self):
        main = self.proto('main.proto', 'syntax="proto2"; import "dep.proto"; message Main {}')
        with self.assertRaisesRegex(ConversionError, '缺失 import'):
            collect_proto_files([main], [self.inputs], self.compiler)
        self.proto('dep.proto')
        second = self.root / 'second'
        self.proto('dep.proto', root=second)
        with self.assertRaisesRegex(ConversionError, '遮蔽'):
            collect_proto_files([main], [self.inputs, second], self.compiler)

    def test_empty_and_unknown_syntax(self):
        for text in ('', '// only comment', 'syntax="proto99"; message X {}'):
            path = self.proto('main.proto', text)
            with self.subTest(text=text), self.assertRaises(ConversionError):
                collect_proto_files([path], [self.inputs], self.compiler)

    def test_inspection_and_failure_never_publish(self):
        self.fake_compiler()
        result = convert(self.request(action='inspect'))
        self.assertIsNone(result.output_path)
        self.assertEqual(result.java_count, 1)
        self.assertEqual(list(self.output.iterdir()), [])
        self.fake_compiler(fail=True)
        with self.assertRaises(ConversionError) as caught:
            convert(self.request())
        self.assertEqual(caught.exception.code, 'COMPILATION_FAILED')
        self.assertEqual(caught.exception.line, 2)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_event_stages_and_cancel_before_export(self):
        self.fake_compiler()
        token = CancellationToken()
        stages = []
        def event(**fields):
            stages.append(fields['stage'])
            if fields['stage'] == 'exporting':
                token.cancel()
        with self.assertRaises(Cancelled):
            convert(self.request(), event, token)
        self.assertEqual(stages, ['reading', 'parsing', 'validating', 'generating', 'exporting'])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cancel_running_child(self):
        self.fake_compiler(sleep=True)
        token = CancellationToken()
        timer = threading.Timer(0.3, token.cancel)
        timer.start()
        self.addCleanup(timer.cancel)
        before = time.monotonic()
        with self.assertRaises(Cancelled):
            convert(self.request(), cancellation=token)
        self.assertLess(time.monotonic() - before, 3)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_engine_sigterm_cleans_staging_and_child(self):
        self.fake_compiler(sleep=True)
        source = self.proto('main.proto')
        pid_file = self.root / 'compiler.pid'
        compiler_text = self.compiler.read_text().replace('time.sleep(30)',
            f'pathlib.Path({str(pid_file)!r}).write_text(str(__import__("os").getpid()))\ntime.sleep(30)')
        self.compiler.write_text(compiler_text)
        wrapper = self.root / 'engine_fixture.py'
        wrapper.write_text(f'''import sys
sys.path.insert(0, {str(ROOT)!r})
import proto_tool.service as service
from proto_tool.toolchain import COMPILER_VERSIONS, RUNTIME_VERSION, find_protoc, setup
from proto_tool.engine import main
service.find_protoc = lambda explicit=None, cancellation=None, target_runtime='3.25.9': find_protoc({str(self.compiler)!r}, cancellation, target_runtime)
raise SystemExit(main())
''')
        process = subprocess.Popen([sys.executable, str(wrapper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        payload = dict(protocol_version=1, task_id='cancel', action='convert', input_kind='proto',
                       input_paths=[str(source)], include_paths=[], output_parent=str(self.output))
        process.stdin.write(json.dumps(payload) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.01)
        self.assertTrue(pid_file.exists(), 'fixture compiler did not start')
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(stderr, '')
        self.assertEqual(json.loads(stdout.splitlines()[-1])['code'], 'CANCELLED')
        self.assertEqual(list(self.output.iterdir()), [])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_publication_ignores_late_cancel(self):
        token = CancellationToken()
        with token.publication():
            token.cancel()
        token.check()

    def test_html_exclusion_can_be_disabled(self):
        source = self.root / 'document.html'
        body = '// $(FILE)a.proto\nsyntax="proto2"; message A {}\n// $(FILE)his_encrypt_data.proto\nsyntax="proto2"; message B {}'
        source.write_text('<div title="测试"><pre>' + html.escape(body) + '</pre></div>')
        self.fake_compiler()
        request = ConversionRequest('html', [source], output_parent=self.output, protoc=self.compiler)
        result = convert(request)
        self.assertEqual(result.proto_count, 1)
        self.assertTrue(result.excluded)
        request.exclude_opt_0081 = False
        self.assertEqual(convert(request).proto_count, 2)

    def test_repair_hash_mismatch_reports_warning(self):
        config = self.root / 'repairs.json'
        config.write_text(json.dumps({'a.proto': {'sha256': '0' * 64}}))
        warnings = []
        repaired, changes = repair_sources({'a.proto': Source('a.proto', 'syntax="proto2"; message A {}')}, config, warnings=warnings)
        self.assertEqual(changes, [])
        self.assertIn('校验不匹配', warnings[0])

    def test_frozen_compiler_never_uses_path_or_explicit(self):
        self.fake_compiler()
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(self.root), create=True), patch('shutil.which', side_effect=AssertionError('PATH must not be used')):
            compiler, version = find_protoc('/not/used')
        self.assertEqual(compiler, self.compiler.resolve())
        self.assertEqual(version, 'libprotoc 25.9')

    def test_protocol_shape_and_chinese_errors(self):
        for payload, code in [('{', 'INVALID_JSON'), ('{}', 'UNSUPPORTED_PROTOCOL')]:
            stdout = io.StringIO()
            self.assertEqual(main(io.StringIO(payload), stdout), 1)
            error = json.loads(stdout.getvalue())
            self.assertEqual(error['protocol_version'], 1)
            self.assertEqual(error['code'], code)
            self.assertIn('message_zh', error)
            self.assertNotIn('Traceback', error['message_zh'])
        with self.assertRaises(ConversionError):
            parse_request(dict(protocol_version=1, task_id='t', action='convert', input_kind='proto', input_paths=['relative'], include_paths=[]))

    def test_protocol_result_events_are_ndjson(self):
        self.fake_compiler()
        source = self.proto('main.proto')
        payload = dict(protocol_version=1, task_id='任务 1', action='convert', input_kind='proto', input_paths=[str(source)], include_paths=[], output_parent=str(self.output))
        stdout = io.StringIO()
        with patch('proto_tool.service.find_protoc', return_value=(self.compiler, 'libprotoc 25.9')):
            self.assertEqual(main(io.StringIO(json.dumps(payload)), stdout), 0)
        messages = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertTrue(all(m['protocol_version'] == 1 and m['task_id'] == '任务 1' for m in messages))
        self.assertEqual(messages[-1]['type'], 'result')
        self.assertEqual(messages[-1]['proto_count'], 1)
        self.assertNotIn('result', messages[-1])

    def test_version_result_has_empty_arrays_and_no_output(self):
        self.fake_compiler()
        payload = dict(protocol_version=1, task_id='version', action='version', input_kind='proto',
                       input_paths=[], include_paths=[])
        stdout = io.StringIO()
        with patch('proto_tool.service.find_protoc', return_value=(self.compiler, 'libprotoc 25.9')):
            self.assertEqual(main(io.StringIO(json.dumps(payload)), stdout), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual((result['proto_count'], result['java_count'], result['output_path']), (0, 0, None))
        for key in ('warnings', 'changes', 'excluded', 'source_files'):
            self.assertEqual(result[key], [])
        self.assertEqual(result['runtime_version'], '3.25.9')

    def test_runtime_profile_reaches_conversion_version_and_cli(self):
        for runtime in ('3.25.9', '3.6.1'):
            with self.subTest(runtime=runtime):
                self.fake_compiler(runtime=runtime)
                request = self.request(target_runtime=runtime)
                result = convert(request)
                self.assertEqual(result.runtime_version, runtime)
                self.assertEqual(result.compiler_version, 'libprotoc ' + COMPILER_VERSIONS[runtime])
                request.action = 'version'
                self.assertEqual(convert(request).runtime_version, runtime)
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(cli_main(['compile', str(request.input_paths[0]), '--protoc', str(self.compiler),
                                               '--target-runtime', runtime, '-o', str(self.output / runtime)]), 0)
                self.assertIn('com.google.protobuf:protobuf-java:' + runtime, stdout.getvalue())

    def test_runtime_profile_validation_and_explicit_mismatch(self):
        self.fake_compiler()
        for runtime in ('3.11.0', '', None, [], True):
            with self.subTest(runtime=runtime):
                with self.assertRaisesRegex(ConversionError, '运行库版本'):
                    convert(self.request(target_runtime=runtime))
                with self.assertRaisesRegex(ProtoError, '运行库版本'):
                    find_protoc(self.compiler, target_runtime=runtime)
        with self.assertRaisesRegex(ProtoError, '版本不匹配'):
            convert(self.request(target_runtime='3.6.1'))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_protocol_runtime_default_selection_and_invalid_values(self):
        payload = dict(protocol_version=1, task_id='runtime', action='version', input_kind='proto',
                       input_paths=[], include_paths=[])
        self.assertEqual(parse_request(payload).target_runtime, '3.25.9')
        payload['target_runtime'] = '3.6.1'
        self.assertEqual(parse_request(payload).target_runtime, '3.6.1')
        stdout = io.StringIO()
        with patch('proto_tool.service.find_protoc', return_value=(self.compiler, 'libprotoc 3.6.1')) as finder:
            self.assertEqual(main(io.StringIO(json.dumps(payload)), stdout), 0)
        self.assertEqual(finder.call_args.kwargs['target_runtime'], '3.6.1')
        self.assertEqual(json.loads(stdout.getvalue())['runtime_version'], '3.6.1')
        for runtime in ('3.11.0', None, [], True):
            payload['target_runtime'] = runtime
            stdout = io.StringIO()
            self.assertEqual(main(io.StringIO(json.dumps(payload)), stdout), 1)
            self.assertEqual(json.loads(stdout.getvalue())['code'], 'INVALID_REQUEST')

    def test_frozen_legacy_profile_uses_own_toolchain(self):
        self.fake_compiler(runtime='3.6.1')
        legacy = self.root / 'toolchains/3.6.1/bin/protoc'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(self.compiler.read_bytes())
        legacy.chmod(0o755)
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(self.root), create=True), patch('shutil.which', side_effect=AssertionError('PATH must not be used')):
            compiler, version = find_protoc('/not/used', target_runtime='3.6.1')
        self.assertEqual(compiler, legacy.resolve())
        self.assertEqual(version, 'libprotoc 3.6.1')

    def test_setup_and_setup_cli_pass_runtime_to_bootstrap(self):
        bootstrap = self.root / 'scripts/bootstrap.py'
        bootstrap.parent.mkdir()
        bootstrap.touch()
        native = self.root / '.tools/native-protoc/3.6.1/arm64/bin/protoc'
        with patch('proto_tool.toolchain.ROOT', self.root), patch('platform.system', return_value='Darwin'), patch('platform.machine', return_value='arm64'), patch('proto_tool.toolchain.run') as runner, patch('proto_tool.toolchain.find_protoc', return_value=(native, 'libprotoc 3.6.1')) as finder:
            self.assertEqual(setup('3.6.1'), native)
            runner.assert_called_once_with([sys.executable, bootstrap, '--target-runtime', '3.6.1', '--arch', 'arm64'])
            finder.assert_called_once_with(native, target_runtime='3.6.1')
        with patch('proto_tool.cli.setup', return_value=native) as prepare, redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(['setup', '--target-runtime', '3.6.1']), 0)
        prepare.assert_called_once_with('3.6.1')

    def test_cli_default_output_stays_protected(self):
        self.fake_compiler()
        source = self.proto('main.proto')
        command = [sys.executable, str(ROOT / 'proto_compile.py'), 'compile', str(source), '--protoc', str(self.compiler)]
        first = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        sentinel = self.root / 'output/user.txt'
        sentinel.write_text('保留原数据')
        second = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('不覆盖已有文件', second.stderr)
        self.assertEqual(sentinel.read_text(), '保留原数据')


    def inspected(self, request=None):
        request = request or self.request()
        parent = self.root / 'private-session'
        parent.mkdir(exist_ok=True)
        request = replace(request, action='inspect', preparation_output=parent / uuid.uuid4().hex,
                          preparation_path=None)
        return request, convert(request)

    def export_request(self, request, result, **changes):
        return replace(request, action='convert', preparation_output=None, preparation_path=result.preparation_path,
                       **changes)

    def test_preparation_reuses_generator_and_preserves_repeat_exports(self):
        self.fake_compiler()
        request, inspected = self.inspected()
        cache = Path(inspected.preparation_path)
        self.assertIsNone(inspected.output_path)
        self.assertEqual({p.name for p in cache.iterdir()}, {'java', 'proto', 'manifest.json'})
        with patch('proto_tool.service._compile_collected', side_effect=AssertionError('generator must not run on hit')):
            first = convert(self.export_request(request, inspected))
            second = convert(self.export_request(request, inspected))
        self.assertNotEqual(first.output_path, second.output_path)
        self.assertTrue(cache.is_dir())
        for result in (first, second):
            self.assertEqual({p.name for p in Path(result.output_path).iterdir()}, {'java', 'proto'})
            for key in ('proto_count', 'java_count', 'compiler_version', 'runtime_version', 'warnings', 'changes', 'excluded', 'source_files'):
                self.assertEqual(getattr(result, key), getattr(inspected, key))

    def test_preparation_detects_same_size_same_timestamp_source_change(self):
        self.fake_compiler()
        request, inspected = self.inspected()
        source = request.input_paths[0]
        before = source.stat()
        source.write_text(source.read_text().replace('message A', 'message B'))
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(source.stat().st_size, before.st_size)
        self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)
        with patch('proto_tool.service._compile_collected', wraps=service._compile_collected) as generate:
            result = convert(self.export_request(request, inspected))
        self.assertEqual(generate.call_count, 1)
        self.assertIn('message B', (Path(result.output_path) / 'proto/main.proto').read_text())

    def test_preparation_tracks_dependencies_and_standard_include_contents(self):
        self.fake_compiler()
        main = self.proto('main.proto', 'syntax="proto3"; import "dep.proto"; import "google/protobuf/empty.proto"; message Main {}')
        dependency = self.proto('dep.proto', 'syntax="proto3"; message Dep {}')
        standard = self.proto('google/protobuf/empty.proto', 'syntax="proto3"; message Empty {}', self.standard)
        request, inspected = self.inspected(self.request([main]))
        manifest = json.loads((Path(inspected.preparation_path) / 'manifest.json').read_text())
        self.assertIn(str(standard.resolve()), manifest['snapshot']['files'])
        for changed in (dependency, standard):
            changed.write_text(changed.read_text() + '\n// changed')
            with patch('proto_tool.service._compile_collected', wraps=service._compile_collected) as generate:
                result = convert(self.export_request(request, inspected))
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(result.proto_count, 2)
            self.assertFalse((Path(result.output_path) / 'proto/google').exists())

    def test_preparation_detects_new_include_shadow_and_directory_entries(self):
        self.fake_compiler()
        main = self.proto('main.proto', 'syntax="proto2"; import "dep.proto"; message Main {}')
        self.proto('dep.proto', 'syntax="proto2"; message Dep {}')
        extra = self.root / 'include-extra'
        extra.mkdir()
        request, inspected = self.inspected(self.request([main], include_paths=[extra]))
        self.proto('dep.proto', root=extra)
        with self.assertRaisesRegex(ConversionError, '遮蔽'):
            convert(self.export_request(request, inspected))
        self.assertEqual(list(self.output.iterdir()), [])
        request, inspected = self.inspected(self.request([self.inputs]))
        self.proto('new.proto', 'syntax="proto2"; message New {}')
        with patch('proto_tool.service._compile_collected', wraps=service._compile_collected) as generate:
            result = convert(self.export_request(request, inspected))
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(result.proto_count, 3)

    def test_preparation_ignores_unrelated_include_files(self):
        self.fake_compiler()
        extra = self.root / 'unrelated-include'
        extra.mkdir()
        unused = self.proto('unused.proto', root=extra)
        request, inspected = self.inspected(self.request(include_paths=[extra]))
        unused.write_text('this is not even valid protobuf')
        self.proto('another-unused.proto', root=extra)
        with patch('proto_tool.service._compile_collected', side_effect=AssertionError('unrelated files must not invalidate preparation')):
            convert(self.export_request(request, inspected))

    def test_html_preparation_skips_parsing_and_tracks_exclusion_and_repairs(self):
        self.fake_compiler()
        source = self.root / 'document.html'
        body = '// $(FILE)a.proto\nsyntax="proto2"; message A {}\n// $(FILE)his_encrypt_data.proto\nsyntax="proto2"; message B {}'
        source.write_text('<div title="测试"><pre>' + html.escape(body) + '</pre></div>')
        rules = self.root / 'rules.json'
        rules.write_text('{}')
        request, inspected = self.inspected(ConversionRequest('html', [source], output_parent=self.output,
                                                             protoc=self.compiler, repairs=rules))
        with patch('proto_tool.service.prepare_html', side_effect=AssertionError('HTML must not be parsed on cache hit')), patch('proto_tool.service._compile_collected', side_effect=AssertionError('generator must not run')):
            convert(self.export_request(request, inspected))
        for changes, count in ((dict(exclude_opt_0081=False), 2), (dict(no_repairs=True), 1), (dict(java_package='com.example'), 1)):
            with patch('proto_tool.service._compile_collected', wraps=service._compile_collected) as generate:
                result = convert(self.export_request(request, inspected, **changes))
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(result.proto_count, count)
        rules.write_text('{ }')
        with patch('proto_tool.service._compile_collected', wraps=service._compile_collected) as generate:
            convert(self.export_request(request, inspected))
        self.assertEqual(generate.call_count, 1)

    def test_preparation_detects_runtime_and_compiler_identity_change(self):
        self.fake_compiler()
        request, inspected = self.inspected()
        self.compiler.write_text(self.compiler.read_text() + '\n# compiler changed\n')
        with patch('proto_tool.service._compile_collected', wraps=service._compile_collected) as generate:
            convert(self.export_request(request, inspected))
        self.assertEqual(generate.call_count, 1)
        self.fake_compiler(runtime='3.6.1')
        with patch('proto_tool.service._compile_collected', wraps=service._compile_collected) as generate:
            result = convert(self.export_request(request, inspected, target_runtime='3.6.1'))
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(result.runtime_version, '3.6.1')

    def test_preparation_corruption_and_symlinks_fall_back(self):
        self.fake_compiler()
        for damage in ('artifact', 'metadata', 'unknown-version', 'malformed', 'symlink', 'extra-file'):
            with self.subTest(damage=damage):
                request, inspected = self.inspected()
                root = Path(inspected.preparation_path)
                manifest = root / 'manifest.json'
                if damage == 'artifact':
                    next((root / 'java').rglob('*.java')).write_text('damaged')
                elif damage in ('metadata', 'unknown-version'):
                    data = json.loads(manifest.read_text())
                    if damage == 'metadata':
                        data['result']['warnings'] = ['被篡改的警告']
                    else:
                        data['format_version'] = 99
                    manifest.write_text(json.dumps(data))
                elif damage == 'malformed':
                    manifest.write_text('{')
                elif damage == 'symlink':
                    artifact = next((root / 'java').rglob('*.java'))
                    artifact.unlink()
                    artifact.symlink_to(request.input_paths[0])
                else:
                    (root / 'user.txt').write_text('unrecognized content')
                with patch('proto_tool.service._compile_collected', wraps=service._compile_collected) as generate:
                    result = convert(self.export_request(request, inspected))
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(result.warnings, inspected.warnings)
                self.assertEqual({p.name for p in Path(result.output_path).iterdir()}, {'java', 'proto'})
                self.assertTrue(root.exists())

    def test_preparation_copy_verification_catches_concurrent_damage(self):
        self.fake_compiler()
        request, inspected = self.inspected()
        original = prepared.copy_verified
        def damage_then_copy(path, target, manifest, token):
            next((Path(path) / 'java').rglob('*.java')).write_text('changed after validation')
            return original(path, target, manifest, token)
        with patch('proto_tool.prepared.copy_verified', side_effect=damage_then_copy), patch('proto_tool.service._compile_collected', wraps=service._compile_collected) as generate:
            result = convert(self.export_request(request, inspected))
        self.assertEqual(generate.call_count, 1)
        self.assertNotIn('changed after validation', next((Path(result.output_path) / 'java').rglob('*.java')).read_text())

    def test_preparation_cancellation_propagates_and_keeps_private_source(self):
        self.fake_compiler()
        request, inspected = self.inspected()
        token = CancellationToken()
        original = prepared.copy_verified
        def cancel_copy(path, target, manifest, cancellation):
            cancellation.cancel()
            return original(path, target, manifest, cancellation)
        with patch('proto_tool.prepared.copy_verified', side_effect=cancel_copy), patch('proto_tool.service._compile_collected', side_effect=AssertionError('cancel must not fall back')):
            with self.assertRaises(Cancelled):
                convert(self.export_request(request, inspected), cancellation=token)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue(Path(inspected.preparation_path).exists())

    def test_preparation_stale_inspection_and_failure_clean_only_owned_staging(self):
        self.fake_compiler()
        request = self.request()
        parent = self.root / 'private-session'
        parent.mkdir()
        sentinel = parent / 'user.txt'
        sentinel.write_text('keep')
        request = replace(request, action='inspect', preparation_output=parent / uuid.uuid4().hex)
        def change_input(**event):
            if event['stage'] == 'generating':
                request.input_paths[0].write_text('syntax="proto2"; message Changed {}')
        with self.assertRaisesRegex(ConversionError, '发生变化'):
            convert(request, change_input)
        self.assertFalse(Path(request.preparation_output).exists())
        self.assertEqual(list(parent.iterdir()), [sentinel])
        self.fake_compiler(fail=True)
        with self.assertRaises(ConversionError):
            convert(request)
        self.assertEqual(list(parent.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_dependency_collection_runs_once_and_observes_standard_files(self):
        self.fake_compiler()
        with patch('proto_tool.service.collect_proto_files', wraps=service.collect_proto_files) as collect:
            convert(self.request())
        self.assertEqual(collect.call_count, 1)
        with tempfile.TemporaryDirectory() as temp:
            source = self.proto('public.proto')
            with patch('proto_tool.service.collect_proto_files', wraps=service.collect_proto_files) as collect:
                service.compile_files(self.compiler, [source], [self.inputs], Path(temp) / 'java')
            self.assertEqual(collect.call_count, 1)

    def test_preparation_protocol_paths_are_action_specific_and_absolute(self):
        payload = dict(protocol_version=1, task_id='p', action='inspect', input_kind='proto', input_paths=[], include_paths=[],
                       preparation_output=str(self.root / 'private'))
        self.assertEqual(parse_request(payload).preparation_output, payload['preparation_output'])
        for field, action, value in (('preparation_output', 'convert', str(self.root / 'p')),
                                     ('preparation_path', 'inspect', str(self.root / 'p')),
                                     ('preparation_output', 'inspect', 'relative')):
            data = dict(payload, action=action)
            data.pop('preparation_output', None)
            data[field] = value
            with self.assertRaises(ConversionError):
                parse_request(data)


    def test_preparation_export_inside_input_directory_ignores_only_owned_staging(self):
        self.fake_compiler()
        self.proto('main.proto')
        request, inspected = self.inspected(self.request([self.inputs]))
        result = convert(self.export_request(request, inspected, output_parent=self.inputs))
        self.assertEqual(result.proto_count, 1)
        self.assertTrue((Path(result.output_path) / 'proto/main.proto').is_file())

    def test_preparation_existing_target_is_never_deleted(self):
        self.fake_compiler()
        target = self.root / 'existing-target'
        target.mkdir()
        sentinel = target / 'user.txt'
        sentinel.write_text('保留')
        request = self.request(action='inspect', preparation_output=target)
        with self.assertRaises(ConversionError):
            convert(request)
        self.assertEqual(sentinel.read_text(), '保留')


    def test_ndjson_inspection_preparation_is_reusable_by_conversion(self):
        self.fake_compiler()
        source = self.proto('main.proto')
        parent = self.root / 'session'
        parent.mkdir()
        payload = dict(protocol_version=1, task_id='inspect-prepared', action='inspect', input_kind='proto',
                       input_paths=[str(source)], include_paths=[], preparation_output=str(parent / uuid.uuid4().hex))
        stdout = io.StringIO()
        with patch('proto_tool.service.find_protoc', return_value=(self.compiler.resolve(), 'libprotoc 25.9')):
            self.assertEqual(main(io.StringIO(json.dumps(payload)), stdout), 0)
            inspected = json.loads(stdout.getvalue().splitlines()[-1])
            self.assertEqual(inspected['preparation_path'], payload['preparation_output'])
            payload.update(action='convert', task_id='export-prepared', output_parent=str(self.output),
                           preparation_path=inspected['preparation_path'])
            del payload['preparation_output']
            stdout = io.StringIO()
            with patch('proto_tool.service._compile_collected', side_effect=AssertionError('cache must reuse through protocol')):
                self.assertEqual(main(io.StringIO(json.dumps(payload)), stdout), 0)
        exported = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual(exported['java_count'], inspected['java_count'])
        self.assertEqual({p.name for p in Path(exported['output_path']).iterdir()}, {'java', 'proto'})

    def test_cancelled_inspection_never_leaves_preparation_target(self):
        self.fake_compiler()
        parent = self.root / 'private-session'
        parent.mkdir()
        for stage in ('generating', 'exporting'):
            token = CancellationToken()
            request = self.request(action='inspect', preparation_output=parent / uuid.uuid4().hex)
            def cancel_at_stage(**event):
                if event['stage'] == stage:
                    token.cancel()
            with self.subTest(stage=stage), self.assertRaises(Cancelled):
                convert(request, cancel_at_stage, token)
            self.assertEqual(list(parent.iterdir()), [])


class BoundedGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bounded-generation-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / 'proto'
        self.inputs.mkdir()

    def compiler(self, runtime):
        try:
            return find_protoc(target_runtime=runtime)[0]
        except ProtoError as exc:
            self.skipTest(str(exc))

    def proto(self, name, content):
        path = self.inputs / name
        path.write_text(content)
        return path

    def generated_hashes(self, root):
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob('*') if path.is_file()}

    def test_batched_generation_preserves_all_outputs_and_warnings(self):
        self.proto('a.proto', 'syntax="proto2"; package a; option java_multiple_files=true; message A {optional int32 value=1;}')
        self.proto('b.proto', 'syntax="proto2"; package b; import public "a.proto"; message B {optional a.A a=1;}')
        self.proto('c.proto', 'syntax="proto2"; package c; import "b.proto"; message C {optional a.A a=1;}')
        for runtime in ('3.25.9', '3.6.1'):
            compiler = self.compiler(runtime)
            logical, roots = collect_proto_files(list(self.inputs.glob('*.proto')), [self.inputs], compiler)
            direct, batched = self.root / ('direct-' + runtime), self.root / ('batched-' + runtime)
            direct.mkdir()
            original = service.run([compiler, *[f'-I{root}' for root in roots], f'--java_out={direct}',
                                    *[str(logical[name]) for name in sorted(logical)]], cwd=roots[0])
            with patch('proto_tool.service.JAVA_BATCH_FILES', 1), patch('proto_tool.service.JAVA_BATCH_SOURCE_BYTES', 1):
                generated = service._compile_collected(compiler, logical, roots, batched)
            self.assertEqual(self.generated_hashes(direct), self.generated_hashes(batched))
            self.assertEqual(original.stderr, generated.stderr)
            self.assertEqual(original.stdout, generated.stdout)
            self.assertFalse(list(self.root.glob('.proto-batches-*')))

    def test_batched_java_output_collision_matches_native_failure(self):
        files = [self.proto('a.proto', 'syntax="proto2"; package a; option java_package="collision"; option java_outer_classname="Shared"; message A {}'),
                 self.proto('b.proto', 'syntax="proto2"; package b; option java_package="collision"; option java_outer_classname="Shared"; message B {}')]
        for runtime in ('3.25.9', '3.6.1'):
            compiler = self.compiler(runtime)
            direct = self.root / ('direct-' + runtime)
            direct.mkdir()
            with self.assertRaises(ProtoError) as original:
                service.run([compiler, f'-I{self.inputs}', f'--java_out={direct}', *map(str, files)])
            java = self.root / ('batched-' + runtime)
            with patch('proto_tool.service.JAVA_BATCH_FILES', 1), patch('proto_tool.service.JAVA_BATCH_SOURCE_BYTES', 1):
                with self.assertRaises(ProtoError) as batched:
                    service.compile_files(compiler, files, [self.inputs], java)
            self.assertEqual(str(original.exception), str(batched.exception))
            self.assertEqual(list(java.iterdir()), [])
            self.assertFalse(list(self.root.glob('.proto-batches-*')))

    def test_batched_global_symbol_conflict_matches_native_failure(self):
        files = [self.proto('a.proto', 'syntax="proto2"; package same; message X {}'),
                 self.proto('b.proto', 'syntax="proto2"; package same; message X {}')]
        for runtime in ('3.25.9', '3.6.1'):
            compiler = self.compiler(runtime)
            direct = self.root / ('direct-' + runtime)
            direct.mkdir()
            with self.assertRaises(ProtoError) as original:
                service.run([compiler, f'-I{self.inputs}', f'--java_out={direct}', *map(str, files)])
            with patch('proto_tool.service.JAVA_BATCH_FILES', 1), patch('proto_tool.service.JAVA_BATCH_SOURCE_BYTES', 1):
                with self.assertRaises(ProtoError) as batched:
                    service.compile_files(compiler, files, [self.inputs], self.root / ('batched-' + runtime))
            self.assertEqual(str(original.exception), str(batched.exception))

    def test_cancellation_between_batches_cleans_and_never_publishes(self):
        for name in ('a', 'b', 'c'):
            self.proto(name + '.proto', f'syntax="proto2"; message {name.upper()} {{}}')
        self.compiler('3.25.9')
        output = self.root / 'exports'
        output.mkdir()
        token = CancellationToken()
        native = service.run
        java_calls = []
        def cancel_second_batch(command, **kwargs):
            if any(str(arg).startswith('--java_out=') for arg in command):
                java_calls.append(command)
                if len(java_calls) == 2:
                    token.cancel()
            return native(command, **kwargs)
        request = ConversionRequest('proto', [self.inputs], output_parent=output)
        with patch('proto_tool.service.JAVA_BATCH_FILES', 1), patch('proto_tool.service.JAVA_BATCH_SOURCE_BYTES', 1), patch('proto_tool.service.run', side_effect=cancel_second_batch):
            with self.assertRaises(Cancelled):
                convert(request, cancellation=token)
        self.assertEqual(len(java_calls), 2)
        self.assertEqual(list(output.iterdir()), [])

    def test_small_task_does_not_add_descriptor_validation(self):
        self.compiler('3.25.9')
        source = self.proto('a.proto', 'syntax="proto2"; message A {}')
        with patch('proto_tool.service.run', wraps=service.run) as native:
            service.compile_files(self.compiler('3.25.9'), [source], [self.inputs], self.root / 'java')
        self.assertEqual(native.call_count, 1)
        self.assertTrue(any(str(arg).startswith('--java_out=') for arg in native.call_args.args[0]))


    @unittest.skipUnless((ROOT / 'device protocol v16.2.html').is_file(), '未提供本地私有协议文档')
    def test_representative_document_batched_outputs_and_diagnostics_exact(self):
        _, normalized, *_ = service.prepare_html(ROOT / 'device protocol v16.2.html')
        service.write_sources(normalized, self.inputs)
        for runtime in ('3.25.9', '3.6.1'):
            compiler = self.compiler(runtime)
            logical, roots = collect_proto_files(sorted(self.inputs.glob('*.proto')), [self.inputs], compiler)
            direct, batched = self.root / ('direct-' + runtime), self.root / ('batched-' + runtime)
            direct.mkdir()
            original = service.run([compiler, *[f'-I{root}' for root in roots], f'--java_out={direct}',
                                    *[str(logical[name]) for name in sorted(logical)]], cwd=roots[0])
            generated = service._compile_collected(compiler, logical, roots, batched)
            self.assertEqual(self.generated_hashes(direct), self.generated_hashes(batched))
            self.assertEqual(original.stderr, generated.stderr)
            self.assertEqual(original.stdout, generated.stdout)


if __name__ == '__main__':
    unittest.main()
