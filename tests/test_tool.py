import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from proto_tool import ProtoError
from proto_tool.cli import output_transaction
from proto_tool.extract import Source, extract, safe_name
from proto_tool.normalize import normalize
from proto_tool.repairs import repair_sources
from proto_tool.toolchain import INSTALL, find_protoc, unpack, verify_archive

ROOT = Path(__file__).resolve().parent.parent
PROTOC = INSTALL / 'bin' / 'protoc'


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

    def test_actual_document(self):
        result = extract(ROOT / 'device protocol v16.2.html')
        self.assertEqual(sum(n.endswith('.proto') for n in result), 58)
        self.assertEqual(len(result['phone_book.proto'].titles), 2)
        self.assertIn('his_encrypt _medic_data.proto', result)


class NormalizationTests(unittest.TestCase):
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
        with patch('proto_tool.toolchain.INSTALL', Path('/nonexistent-protoc-test')), patch('shutil.which', return_value=None):
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


@unittest.skipUnless(PROTOC.is_file(), '先运行 setup 以执行真实 protoc 集成测试')
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
        self.assertEqual(json.loads((self.root / 'directory/report.json').read_text())['proto_count'], 2)

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
        self.assertIn('default=A_SAME', (self.root / 'out/normalized/a.proto').read_text())
        self.assertIn('default=SAME', (self.root / 'out/original/a.proto').read_text())

    def test_document_excluding_opt_0081(self):
        result = self.call(ROOT / 'device protocol v16.2.html', '-o', self.root / 'out', '--exclude-opt', 'OPT_0081')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.root / 'out/report.json').read_text())
        self.assertEqual(report['proto_count'], 57)
        self.assertEqual(report['java_count'], 57)
        self.assertEqual(report['excluded'][0]['file'], 'his_encrypt_data.proto')
        self.assertTrue((self.root / 'out/original/his_encrypt_data.proto').exists())
        self.assertFalse((self.root / 'out/normalized/his_encrypt_data.proto').exists())
        self.assertFalse((self.root / 'out/java/device/protocol/his_encrypt_data').exists())
        self.assertTrue((self.root / 'out/normalized/his_encrypt_health_data.proto').exists())

    def test_exclusion_is_html_only(self):
        source = self.root / 'a.proto'
        source.write_text('syntax="proto2"; message A {}')
        result = self.call(source, '-o', self.root / 'out', '--exclude-opt', 'OPT_0081')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('仅用于 HTML', result.stderr)
        self.assertFalse((self.root / 'out').exists())


if __name__ == '__main__':
    unittest.main()
