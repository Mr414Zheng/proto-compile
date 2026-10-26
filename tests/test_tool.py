import hashlib
import gc
import html
import json
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import weakref
from unittest.mock import patch
import zipfile

from proto_tool import ProtoError
from proto_tool.cli import output_transaction
from proto_tool.extract import Source, extract, safe_name
from proto_tool.normalize import Schema, normalize
from proto_tool.repairs import repair_sources
from proto_tool.toolchain import INSTALL, VERSION, RUNTIME_VERSION, find_protoc, unpack, verify_archive

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DOCUMENT = ROOT / 'device protocol v16.2.html'
requires_private_document = unittest.skipUnless(PRIVATE_DOCUMENT.is_file(), '未提供本地私有协议文档')
NATIVE_PROTOC = ROOT / '.tools' / 'native-protoc' / RUNTIME_VERSION / platform.machine() / 'bin' / 'protoc'
PROTOC = NATIVE_PROTOC if NATIVE_PROTOC.is_file() else INSTALL / 'bin' / 'protoc'


def compiler_available(path=PROTOC, version=VERSION):
    if not path.is_file():
        return False
    try:
        result = subprocess.run([str(path), '--version'], capture_output=True, text=True)
        return result.returncode == 0 and result.stdout.strip() == 'libprotoc ' + version
    except OSError:
        return False


def sources(**items):
    return {name: Source(name, 'syntax = "proto2";\n' + text, [name]) for name, text in items.items()}


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def wiki(self, items):
        path = self.root / 'document with spaces.html'
        path.write_text(''.join(f'<div title="{html.escape(t)}"><pre>{html.escape(s)}</pre></div>' for t, s in items), encoding='utf-8')
        return path

    def test_entities_boundaries_duplicates_and_space_names(self):
        body = '// $(FILE)a space.proto\nsyntax = "proto2";\n// a < b & c\nmessage A {}\n// $(FILE)a.options\nA.x max_size:10\n'
        extracted = extract(self.wiki([('标题', body), ('Draft', body)]))
        self.assertEqual(set(extracted), {'a space.proto', 'a.options'})
        self.assertIn('// a < b & c', extracted['a space.proto'].text)
        self.assertNotIn('max_size', extracted['a space.proto'].text)
        self.assertEqual(extracted['a space.proto'].titles, ['标题', 'Draft'])

    def test_conflicting_duplicate(self):
        path = self.wiki([('one', '// $(FILE)a.proto\nmessage A {}'), ('two', '// $(FILE)a.proto\nmessage B {}')])
        with self.assertRaisesRegex(ProtoError, '内容冲突'):
            extract(path)

    def test_json_store(self):
        path = self.root / 'store.html'
        path.write_text('<script class="tiddlywiki-tiddler-store" type="application/json">' + json.dumps([{'title': 'one', 'text': '// $(FILE)a.proto\nsyntax="proto2"; // < &'}]) + '</script>')
        self.assertIn('// < &', extract(path)['a.proto'].text)

    def test_unsafe_paths(self):
        for name in ['../a.proto', '/a.proto', 'a/../../x', 'a\\b', 'C:/x', './a', 'a//b']:
            with self.subTest(name=name), self.assertRaises(ProtoError):
                safe_name(name)
        with self.assertRaises(ProtoError):
            extract(self.wiki([('bad', '// $(FILE)../a.proto\nmessage A {}')]))

    def test_empty_and_case_collision(self):
        with self.assertRaisesRegex(ProtoError, '未找到'):
            extract(self.wiki([('empty', 'hello')]))
        with self.assertRaisesRegex(ProtoError, '大小写冲突'):
            extract(self.wiki([('one', '// $(FILE)a.proto\nmessage A {}\n// $(FILE)A.proto\nmessage B {}')]))

    @requires_private_document
    def test_actual_document(self):
        result = extract(ROOT / 'device protocol v16.2.html')
        self.assertEqual(sum(n.endswith('.proto') for n in result), 58)
        self.assertEqual(len(result['phone_book.proto'].titles), 2)
        self.assertIn('his_encrypt _medic_data.proto', result)
        # 固定完整来源顺序、正文与标题，防止分块提取改变修正规则依赖的原始字节。
        payload = [(name, source.text, source.titles) for name, source in result.items()]
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(digest, '10a8232509d6db7a2f4ec80ddb9dec36c616611e385d23b9c3a9126dc7f8e6c3')

    def test_streaming_entities_utf8_and_tags_at_chunk_boundaries(self):
        path = self.root / '分块文档.html'
        body = '// $(FILE)中文.proto\nsyntax="proto2"; // 中文 &lt; &#x1F600; &amp;\nmessage A {}'
        path.write_text('x' * 8191 + '<div title="中文标题"><pre>' + body + '</pre></div>', encoding='utf-8-sig')
        expected = html.unescape(body.split('\n', 1)[1]).strip() + '\n'
        for chunk in (1, 2, 3, 5, 7, 31, 64, 8192):
            with self.subTest(chunk=chunk), patch('proto_tool.extract.READ_CHUNK_SIZE', chunk):
                result = extract(path)
                self.assertEqual(list(result), ['中文.proto'])
                self.assertEqual(result['中文.proto'].text, expected)
                self.assertEqual(result['中文.proto'].titles, ['中文标题'])

    def test_streaming_mixed_store_order_and_ignored_system_entries(self):
        path = self.root / 'mixed.html'
        body = '// $(FILE)a.proto\nsyntax="proto2"; // &amp; 中文\nmessage A {}'
        store = json.dumps([{'title': 'JSON标题', 'text': body}, {'title': '$:/ignored', 'text': '// $(FILE)../bad.proto'}], ensure_ascii=False)
        path.write_text('<div title="$:/system"><pre>// $(FILE)../bad.proto</pre></div>'
                        '<div title="HTML标题"><pre>' + html.escape(body) + '</pre></div>'
                        '<script class="tiddlywiki-tiddler-store" type="application/json">' + store + '</script>')
        for chunk in (1, 7, 64):
            with self.subTest(chunk=chunk), patch('proto_tool.extract.READ_CHUNK_SIZE', chunk):
                result = extract(path)
                self.assertEqual(result['a.proto'].text, body.split('\n', 1)[1] + '\n')
                self.assertEqual(result['a.proto'].titles, ['HTML标题', 'JSON标题'])

    def test_streaming_preserves_json_error_and_error_precedence(self):
        path = self.root / 'invalid-store.html'
        prefix = '<div title="非法路径"><pre>// $(FILE)../bad.proto</pre></div>'
        for store in ('[', '[{"text": "missing title"}]'):
            path.write_text(prefix + '<script class="tiddlywiki-tiddler-store" type="application/json">' + store + '</script>')
            with self.subTest(store=store), patch('proto_tool.extract.READ_CHUNK_SIZE', 3):
                with self.assertRaisesRegex(ProtoError, 'TiddlyWiki JSON 数据无效'):
                    extract(path)


class NormalizationTests(unittest.TestCase):
    def test_public_import_cycle_and_schema_release_without_gc(self):
        items = sources(**{'a.proto': 'import public "b.proto"; message A {}',
                           'b.proto': 'import public "a.proto"; message B { optional A a=1; }',
                           'c.proto': 'import "a.proto"; message C { optional B b=1; }'})
        references = []
        def tracked(source, **kwargs):
            schema = Schema(source, **kwargs)
            references.append(weakref.ref(schema))
            return schema
        enabled = gc.isenabled()
        gc.disable()
        try:
            with patch('proto_tool.normalize.Schema', side_effect=tracked):
                normalized, _ = normalize(items)
            self.assertIn('.device_protocol.b.B b', normalized['c.proto'].text)
            self.assertTrue(all(reference() is None for reference in references))
        finally:
            if enabled:
                gc.enable()

    def test_cross_file_local_shadow_and_comments(self):
        items = sources(**{'a.proto': 'message Shared {}', 'b.proto': 'import "a.proto"; message B { optional Shared item = 1; } // Shared\n', 'c.proto': 'import "a.proto"; message Shared {} message C { optional Shared item=1; }'})
        normalized, _ = normalize(items)
        self.assertIn('.device_protocol.a.Shared item', normalized['b.proto'].text)
        self.assertIn('.device_protocol.c.Shared item', normalized['c.proto'].text)
        self.assertTrue(normalized['b.proto'].text.endswith('// Shared\n'))

    def test_nested_scope(self):
        normalized, _ = normalize(sources(**{'a.proto': 'message A { message B {} optional B b = 1; } message C { optional A.B b=1; }'}))
        self.assertEqual(normalized['a.proto'].text.count('.device_protocol.a.A.B'), 2)

    def test_shadowed_missing_nested_type_is_not_guessed(self):
        items = sources(**{'a.proto': 'message Nested { message X {} }', 'b.proto': 'import "a.proto"; message C { message Nested {} optional Nested.X x=1; }'})
        with self.assertRaisesRegex(ProtoError, '遮蔽'):
            normalize(items)

    def test_ambiguous_reference(self):
        items = sources(**{'a.proto': 'message X {}', 'b.proto': 'message X {}', 'c.proto': 'import "a.proto"; import "b.proto"; message C { optional X x=1; }'})
        with self.assertRaisesRegex(ProtoError, '不唯一'):
            normalize(items)

    def test_missing_import_and_unresolved_type(self):
        with self.assertRaisesRegex(ProtoError, '缺失 import'):
            normalize(sources(**{'a.proto': 'import "missing.proto"; message A {}'}))
        with self.assertRaisesRegex(ProtoError, '无法解析类型'):
            normalize(sources(**{'a.proto': 'message A { optional Missing x=1; }'}))

    def test_enum_collision_and_default(self):
        text = 'enum A { SAME=0; } enum B { SAME=3; } message M { optional A a=1 [default=SAME]; optional B b=2 [default=SAME]; optional string text=3 [default="SAME"]; } // SAME'
        normalized, changes = normalize(sources(**{'a.proto': text}))
        result = normalized['a.proto'].text
        self.assertIn('A_SAME=0', result)
        self.assertIn('B_SAME=3', result)
        self.assertIn('default=A_SAME', result)
        self.assertIn('default=B_SAME', result)
        self.assertIn('default="SAME"', result)
        self.assertTrue(result.endswith('// SAME'))
        self.assertEqual(sum(c['kind'] == 'enum_default' for c in changes), 2)

    def test_enum_default_across_import(self):
        items = sources(**{'a.proto': 'enum A { SAME=0; } enum B { SAME=1; }', 'b.proto': 'import "a.proto"; message M { optional A a=1 [default=SAME]; }'})
        normalized, _ = normalize(items)
        self.assertIn('default=A_SAME', normalized['b.proto'].text)

    def test_public_import(self):
        items = sources(**{'a.proto': 'message X {}', 'b.proto': 'import public "a.proto";', 'c.proto': 'import "b.proto"; message C { optional X x=1; }'})
        self.assertIn('.device_protocol.a.X', normalize(items)[0]['c.proto'].text)
        items['b.proto'].text = items['b.proto'].text.replace('public ', '')
        with self.assertRaisesRegex(ProtoError, '无法解析'):
            normalize(items)

    def test_original_packages_and_absolute_name(self):
        items = sources(**{'a.proto': 'package alpha; message X {}', 'b.proto': 'package beta; import "a.proto"; message Y { optional alpha.X a=1; optional .alpha.X b=2; }'})
        self.assertEqual(normalize(items)[0]['b.proto'].text.count('.device_protocol.a.X'), 2)

    def test_package_validation_and_collision(self):
        with self.assertRaisesRegex(ProtoError, '无效 Java'):
            normalize(sources(**{'a.proto': 'message A {}'}), 'bad.class')
        with self.assertRaisesRegex(ProtoError, '命名空间冲突'):
            normalize(sources(**{'a-b.proto': 'message A {}', 'a_b.proto': 'message B {}'}))

    def test_custom_java_package(self):
        normalized, _ = normalize(sources(**{'a.proto': 'message A {}'}), 'com.example')
        self.assertIn('option java_package = "com.example.a";', normalized['a.proto'].text)

    def test_no_java_package_keeps_proto_namespace(self):
        normalized, changes = normalize(sources(**{'a.proto': 'message A {}'}), '')
        self.assertIn('option java_package = "";', normalized['a.proto'].text)
        self.assertIn('package device_protocol.a;', normalized['a.proto'].text)
        self.assertEqual(changes[0]['java_package'], '')

    def test_objc_prefixes_are_stable_unique_and_preserve_explicit_options(self):
        items = sources(**{'a.proto': 'package a; enum State { OK=0; } message Shared {}',
                           'b.proto': 'package b; import "a.proto"; message Shared {optional a.Shared peer=1;}',
                           'custom.proto': 'option objc_class_prefix="USR"; message Shared {}'})
        normalized, _ = normalize(items, output_language='objc')
        again, _ = normalize(dict(reversed(list(items.items()))), output_language='objc')
        self.assertEqual({k: v.text for k, v in normalized.items()}, {k: v.text for k, v in again.items()})
        for name, prefix in (('a.proto', 'PCTa_'), ('b.proto', 'PCTb_'), ('custom.proto', 'USR')):
            self.assertIn(f'option objc_class_prefix = "{prefix}";' if prefix != 'USR' else 'option objc_class_prefix="USR";', normalized[name].text)
            self.assertEqual(normalized[name].text.count('objc_class_prefix'), 1)
            self.assertNotIn('java_package', normalized[name].text)
        self.assertIn('.device_protocol.a.Shared peer', normalized['b.proto'].text)

    def test_objc_option_handling_preserves_protoc_diagnostics(self):
        item = sources(**{'a.proto': 'option objc_class_prefix=""; option java_package="old"; message A {}'})
        normalized, _ = normalize(item, output_language='objc')
        self.assertIn('option objc_class_prefix="";', normalized['a.proto'].text)
        self.assertNotIn('PCTa_', normalized['a.proto'].text)
        with self.assertRaisesRegex(ProtoError, '已有 java_'):
            normalize(item)
        with self.assertRaisesRegex(ProtoError, '输出语言'):
            normalize(item, output_language='swift')


class SafetyTests(unittest.TestCase):
    def test_publication_and_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'out'
            with self.assertRaises(RuntimeError):
                with output_transaction(destination) as staged:
                    (staged / 'partial').write_text('partial')
                    raise RuntimeError('failed')
            self.assertFalse(destination.exists())
            with output_transaction(destination) as staged:
                (staged / 'complete').write_text('ok')
            with self.assertRaises(ProtoError):
                with output_transaction(destination):
                    pass
            self.assertEqual((destination / 'complete').read_text(), 'ok')

    def test_checksum_and_archive_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('../escaped', 'bad')
            verify_archive(archive, hashlib.sha256(archive.read_bytes()).hexdigest())
            with self.assertRaisesRegex(ProtoError, '校验失败'):
                verify_archive(archive, '0' * 64)
            with self.assertRaises(ProtoError):
                unpack(archive, Path(temp) / 'out')

    def test_missing_compiler(self):
        with patch('proto_tool.toolchain.INSTALL', Path('/nonexistent-protoc-test')), patch('proto_tool.toolchain.ROOT', Path('/nonexistent-protoc-root')), patch('shutil.which', return_value=None):
            with self.assertRaisesRegex(ProtoError, '未找到 protoc'):
                find_protoc()
        with self.assertRaisesRegex(ProtoError, '找不到指定'):
            find_protoc('/nonexistent-protoc-test/protoc')

    def test_output_changed_during_build(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'out'
            with self.assertRaisesRegex(ProtoError, '发生变化'):
                with output_transaction(destination):
                    destination.mkdir()
                    (destination / 'user.txt').write_text('keep')
            self.assertEqual((destination / 'user.txt').read_text(), 'keep')


class RepairTests(unittest.TestCase):
    @requires_private_document
    def test_complete_document_normalization_and_repairs_stay_identical(self):
        original = extract(ROOT / 'device protocol v16.2.html')
        selected = {name: source for name, source in original.items()
                    if name.endswith('.proto') and name != 'his_encrypt_data.proto'}
        repaired, repair_changes = repair_sources(selected)
        normalized, changes = normalize(repaired, '')
        def digest(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(digest([(name, item.text, item.titles) for name, item in normalized.items()]),
                         '5c8119381dc48aa4505b5a87e11b7778ea363dfb6194008812ed228df1ca16a4')
        self.assertEqual(digest(repair_changes), 'df3cba171575908b8751e7d76d0f2fdfb4e0cab7a32d94240476bdfa3db681bc')
        self.assertEqual(digest(changes), 'c95a099c5e6b08c0737c359f3df9b55361af10a0531c45c55d370e6b8ec4591b')

    @requires_private_document
    def test_known_missing_import_preserves_original(self):
        original = extract(ROOT / 'device protocol v16.2.html')
        repaired, changes = repair_sources(original)
        self.assertNotIn('import "yyl_info.proto"', original['device_Configure_info.proto'].text)
        self.assertIn('import "yyl_info.proto"', repaired['device_Configure_info.proto'].text)
        self.assertTrue(any(c['kind'] == 'repair_import' for c in changes))

    def test_custom_type_repair_and_checksum(self):
        items = sources(**{'a.proto': 'message New {} message A { optional Old x=1; } // Old'})
        rule = {'a.proto': {'sha256': hashlib.sha256(items['a.proto'].text.encode()).hexdigest(), 'type_replacements': {'Old': 'New'}}}
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'rules.json'
            config.write_text(json.dumps(rule))
            repaired, changes = repair_sources(items, config, strict=True)
            self.assertIn('optional New x=1', repaired['a.proto'].text)
            self.assertTrue(repaired['a.proto'].text.endswith('// Old'))
            self.assertIn('optional Old x=1', items['a.proto'].text)
            self.assertEqual(changes[0]['kind'], 'repair_type')
            items['a.proto'].text += '\n'
            with self.assertRaisesRegex(ProtoError, '校验不匹配'):
                repair_sources(items, config, strict=True)
            self.assertEqual(repair_sources(items, config)[1], [])

    def test_malformed_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'rules.json'
            for content in ['[]', '{', '{"a.proto": {}}']:
                config.write_text(content)
                with self.assertRaises(ProtoError):
                    repair_sources({}, config, strict=True)


@unittest.skipUnless(compiler_available(), '需要当前架构可运行的 protoc 才能执行真实编译集成测试')
class CompileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='proto test ')
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def call(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'proto_compile.py'), 'compile', *map(str, args), '--protoc', str(PROTOC)], text=True, capture_output=True)

    def test_standalone_and_directory(self):
        source = self.root / 'inputs'
        source.mkdir()
        (source / 'a.proto').write_text('syntax="proto2"; option java_package="example"; message A { required int32 n=1; }')
        result = self.call(source / 'a.proto', '-o', self.root / 'single')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(list((self.root / 'single/java/example').glob('*.java')))
        (source / 'b.proto').write_text('syntax="proto2"; import "a.proto"; message B { optional A a=1; }')
        result = self.call(source, '-o', self.root / 'directory')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list((self.root / 'directory/proto').glob('*.proto'))), 2)
        self.assertEqual({p.name for p in (self.root / 'directory').iterdir()}, {'java', 'proto'})

    def test_multiple_include_roots(self):
        roots = [self.root / str(i) for i in range(3)]
        for root in roots:
            root.mkdir()
        (roots[0] / 'main.proto').write_text('syntax="proto2"; import "a.proto"; import "b.proto"; message Main {optional A a=1; optional B b=2;}')
        (roots[1] / 'a.proto').write_text('syntax="proto2"; message A {}')
        (roots[2] / 'b.proto').write_text('syntax="proto2"; message B {}')
        result = self.call(roots[0] / 'main.proto', '-o', self.root / 'out', '-I', roots[1], '-I', roots[2])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failure_does_not_publish(self):
        source = self.root / 'bad.proto'
        for content in ['syntax="proto2"; import "missing.proto";', 'syntax="proto2"; message Broken {']:
            source.write_text(content)
            result = self.call(source, '-o', self.root / 'out')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / 'out').exists())
            self.assertIn('bad.proto', result.stderr)

    def test_html_real_compilation_with_enum_repair(self):
        source = self.root / 'input.html'
        body = '// $(FILE)a.proto\nsyntax="proto2"; enum A { SAME=0; } enum B { SAME=1; } message M { optional A a=1 [default=SAME]; }'
        source.write_text('<div title="枚举"><pre>' + html.escape(body) + '</pre></div>')
        result = self.call(source, '-o', self.root / 'out', '--java-package', 'com.example')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('default=A_SAME', (self.root / 'out/proto/a.proto').read_text())
        self.assertFalse((self.root / 'out/original').exists())

    @requires_private_document
    def test_document_excluding_opt_0081(self):
        result = self.call(ROOT / 'device protocol v16.2.html', '-o', self.root / 'out')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in (self.root / 'out').iterdir()}, {'java', 'proto'})
        self.assertEqual(len(list((self.root / 'out/proto').glob('*.proto'))), 57)
        generated = list((self.root / 'out/java').glob('*.java'))
        self.assertEqual(len(generated), 57)
        self.assertIn('libprotoc ' + VERSION, result.stdout)
        self.assertIn('com.google.protobuf:protobuf-java:' + RUNTIME_VERSION, result.stdout)
        self.assertFalse((self.root / 'out/proto/his_encrypt_data.proto').exists())
        self.assertTrue((self.root / 'out/proto/his_encrypt_health_data.proto').exists())
        self.assertTrue(all('device.protocol.' not in p.read_text() for p in generated))

    def test_default_compiler_and_runtime_output(self):
        compiler, version = find_protoc()
        self.assertEqual(compiler, PROTOC.resolve())
        self.assertEqual(version, 'libprotoc ' + VERSION)
        source = self.root / 'sample.proto'
        source.write_text('syntax="proto2"; message Sample { optional int32 value=1; }')
        result = self.call(source, '-o', self.root / 'out')
        self.assertEqual(result.returncode, 0, result.stderr)
        generated = next((self.root / 'out/java').glob('*.java')).read_text()
        self.assertNotIn('com.google.protobuf.GeneratedFile', generated)
        self.assertNotIn('com.google.protobuf.RuntimeVersion', generated)

    def test_exclusion_is_html_only(self):
        source = self.root / 'a.proto'
        source.write_text('syntax="proto2"; message A {}')
        result = self.call(source, '-o', self.root / 'out', '--exclude-opt', 'OPT_0081')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('仅用于 HTML', result.stderr)
        self.assertFalse((self.root / 'out').exists())

    @requires_private_document
    def test_document_without_java_package(self):
        result = self.call(ROOT / 'device protocol v16.2.html', '-o', self.root / 'out',
                           '--exclude-opt', 'OPT_0081', '--no-java-package')
        self.assertEqual(result.returncode, 0, result.stderr)
        generated = list((self.root / 'out/java').glob('*.java'))
        self.assertEqual(len(generated), 57)
        for source in generated:
            text = source.read_text()
            self.assertNotIn('device.protocol.', text)
            self.assertNotRegex(text, r'(?m)^\s*package\s+')
        self.assertIn('RealtimeData.', (self.root / 'out/java/DeviceConf.java').read_text())

    @requires_private_document
    def test_default_output_and_extract_are_clean(self):
        document = ROOT / 'device protocol v16.2.html'
        result = subprocess.run([sys.executable, str(ROOT / 'proto_compile.py'), 'compile', str(document)],
                                cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in (self.root / 'output').iterdir()}, {'java', 'proto'})
        self.assertTrue(all(p.suffix in ('.java', '.proto') for p in (self.root / 'output').rglob('*') if p.is_file()))
        extraction = self.root / 'extract-run'
        extraction.mkdir()
        result = subprocess.run([sys.executable, str(ROOT / 'proto_compile.py'), 'extract', str(document)],
                                cwd=extraction, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = list((extraction / 'output/proto').iterdir())
        self.assertEqual(len(extracted), 57)
        self.assertTrue(all(p.suffix == '.proto' for p in extracted))


LEGACY_PROTOC = ROOT / '.tools/native-protoc/3.6.1' / platform.machine() / 'bin/protoc'


@unittest.skipUnless(compiler_available(LEGACY_PROTOC, '3.6.1'), '需要当前架构的 protoc 3.6.1 才能执行兼容模式真实编译')
class LegacyCompileTests(unittest.TestCase):
    def test_legacy_standalone_and_complete_document(self):
        with tempfile.TemporaryDirectory(prefix='proto legacy ') as temp:
            root = Path(temp)
            source = root / 'sample.proto'
            source.write_text('syntax="proto2"; message Sample { optional int32 value=1; }')
            for index, (input_path, count) in enumerate(((source, 1), (ROOT / 'device protocol v16.2.html', 57))):
                if input_path == PRIVATE_DOCUMENT and not PRIVATE_DOCUMENT.is_file():
                    with self.subTest(input='private-document'):
                        self.skipTest('未提供本地私有协议文档')
                    continue
                output = root / str(index)
                result = subprocess.run([sys.executable, str(ROOT / 'proto_compile.py'), 'compile', str(input_path),
                                         '--target-runtime', '3.6.1', '-o', str(output)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('libprotoc 3.6.1', result.stdout)
                self.assertIn('com.google.protobuf:protobuf-java:3.6.1', result.stdout)
                self.assertEqual(len(list((output / 'proto').rglob('*.proto'))), count)
                self.assertEqual(len(list((output / 'java').rglob('*.java'))), count)


if __name__ == '__main__':
    unittest.main()
