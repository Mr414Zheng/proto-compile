import html
import json
import struct
import tempfile
import unittest
from pathlib import Path

from proto_tool import ProtoError
from proto_tool.ble_schema import load_protocol
from proto_tool.ble_decoder import crc16, decode_line


def varint(n):
    n &= (1 << 64) - 1
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128); n >>= 7
    result.append(n)
    return bytes(result)


def scalar(tag, n):
    return varint(tag * 8) + varint(n)


def blob(tag, value):
    return varint(tag * 8 + 2) + varint(len(value)) + value


def frame(opcode, payload):
    return (b'DT' + struct.pack('<HHH', len(payload), crc16(payload), opcode) + payload).hex().upper()


class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def schema(self, code, opcode=1, title='peer information', extra=()):
        items = [('Content', f'* OPT_{opcode:04X} [[{title}]]'), (title, '// $(FILE)selected.proto\nsyntax="proto2";\n' + code), *extra]
        path = self.root / 'selected.html'
        path.write_text(''.join(f'<div title="{html.escape(t)}"><pre>{html.escape(b)}</pre></div>' for t, b in items))
        return load_protocol(path)

    def record(self, payload, schema, opcode=1, direction='up'):
        return decode_line('2026-10-27 10:43:05.123+08:00:' + json.dumps({'id': str(opcode), 'name': direction, 'sentence': payload.hex()}), 'json', schema)

    def test_selected_doc_opcode_and_original_names(self):
        schema = self.schema('message PeerInfoSubsriber { enum Original { Original_ZERO = 0; Original_One = 7; } required bool exact_Bool = 1; optional Original my_enum = 2; required uint32 absent = 3; }', opcode=0x67)
        record = self.record(scalar(1, 0) + scalar(2, 7), schema, opcode=0x67)
        self.assertEqual(record.status, 'decoded')
        self.assertEqual(record.structure['data'], {'exact_Bool': False, 'my_enum': 'Original_One'})
        self.assertEqual(record.structure['timestamp'], '2026-10-27 10:43:05.123+08:00')
        self.assertEqual(record.structure['opcode'], '0x0067')
        self.assertNotIn('frame_validation', record.structure)
        second = self.schema('message PeerInfoSubsriber { optional string different = 1; }', opcode=2)
        self.assertEqual(self.record(blob(1, b'new'), second, opcode=2).structure['data'], {'different': 'new'})
        self.assertIsNone(self.record(scalar(1, 1), second).structure['message'])

    def test_scalar_types_packed_and_nested(self):
        schema = self.schema('''message PeerInfoSubsriber {
          enum Mode { ZERO = 0; ACTIVE = 3; }
          message Child { optional string label = 1; }
          optional int32 negative = 1; optional sint32 zigzag = 2;
          optional sfixed32 signed32 = 3; optional fixed64 unsigned64 = 4;
          optional float decimal = 5; optional double precise = 6;
          optional bytes binary = 7; repeated Mode modes = 8;
          repeated fixed32 counts = 9; optional Child child = 10;
          optional string empty = 11; optional bytes empty_bytes = 12;
        }''')
        payload = scalar(1, -2) + scalar(2, 5) + bytes([29]) + struct.pack('<i', -22)
        payload += bytes([33]) + struct.pack('<Q', 2 ** 64 - 1)
        payload += bytes([45]) + struct.pack('<f', 1.5) + bytes([49]) + struct.pack('<d', 2.25)
        payload += blob(7, b'\x00\xff') + blob(8, bytes([0, 3])) + scalar(8, 3)
        payload += blob(9, struct.pack('<II', 2, 4)) + bytes([77]) + struct.pack('<I', 8)
        payload += blob(10, blob(1, '中文'.encode())) + blob(11, b'') + blob(12, b'')
        record = self.record(payload, schema)
        self.assertEqual(record.structure['data']['negative'], -2)
        self.assertEqual(record.structure['data']['zigzag'], -3)
        self.assertEqual(record.structure['data']['signed32'], -22)
        self.assertEqual(record.structure['data']['unsigned64'], str(2 ** 64 - 1))
        self.assertEqual(record.structure['data']['decimal'], 1.5)
        self.assertEqual(record.structure['data']['precise'], 2.25)
        self.assertEqual(record.structure['data']['binary'], '00FF')
        self.assertEqual(record.structure['data']['modes'], ['ZERO', 'ACTIVE', 'ACTIVE'])
        self.assertEqual(record.structure['data']['counts'], [2, 4, 8])
        self.assertEqual(record.structure['data']['child'], {'label': '中文'})
        self.assertEqual(record.structure['data']['empty'], '')
        self.assertEqual(record.structure['data']['empty_bytes'], '')
        json.dumps(record.structure, allow_nan=False)

    def test_unknown_enum_type_and_field_preserve_encoded_bytes(self):
        schema = self.schema('message PeerInfoSubsriber { enum E { ZERO=0; } optional E state=1; optional Missing unresolved=2; optional fixed32 mismatch=3; }')
        payload = scalar(1, 99) + blob(2, b'\xff') + scalar(3, 1) + blob(42, b'\x01\x02')
        record = self.record(payload, schema)
        self.assertEqual(record.status, 'warning')
        self.assertEqual(record.structure['data']['state'], 99)
        fields = record.structure['data']['_unknown_fields']
        self.assertEqual([f['field_number'] for f in fields], [1, 2, 3, 42])
        self.assertEqual(fields[-1]['encoded_hex'], 'D202020102')
        self.assertTrue(schema.warnings)

    def test_frames_direction_inference_and_ambiguity(self):
        schema = self.schema('message PeerInfoSubsriber { optional fixed32 hash=10; } message PeerInfoNotification { optional bool connect_app=10; }')
        record = decode_line(frame(1, scalar(10, 1)), 'frames', schema)
        self.assertEqual(record.structure['direction'], 'down')
        self.assertEqual(record.structure['message'], 'PeerInfoNotification')
        self.assertTrue(record.structure['interpretation']['inferred'])
        self.assertTrue(record.structure['frame_validation']['crc_valid'])
        ambiguous = self.schema('message PeerInfoSubsriber { optional bool a=1; } message PeerInfoNotification { optional bool b=1; }')
        result = decode_line(frame(1, scalar(1, 1)), 'frames', ambiguous)
        self.assertIsNone(result.structure['direction'])
        self.assertIsNone(result.structure['message'])
        self.assertEqual(len(result.structure['interpretation']['candidates']), 2)
        shared = self.schema('message ConParamsUpdate {optional fixed32 mtu=1;}', opcode=0xFFFF, title='connection params')
        result = decode_line(frame(0xFFFF, b'\x0d\x14\x00\x00\x00'), 'frames', shared)
        self.assertIsNone(result.structure['direction'])
        self.assertEqual(result.structure['message'], 'ConParamsUpdate')
        self.assertEqual(result.structure['data']['mtu'], 20)

    def test_crc_length_prefix_independent_and_original_case(self):
        schema = self.schema('message PeerInfoSubsriber {optional bool value=1;}')
        encoded = frame(1, scalar(1, 1))
        raw = bytearray.fromhex(encoded); raw[0] = 0; raw[2] = 3; raw[4] ^= 1
        source = raw.hex()
        result = decode_line(source, 'frames', schema)
        self.assertEqual(result.original_hex, source)
        self.assertEqual(result.status, 'error')
        validation = result.structure['frame_validation']
        self.assertFalse(validation['prefix_valid'])
        self.assertFalse(validation['length_valid'])
        self.assertFalse(validation['crc_valid'])
        self.assertEqual(result.structure['data']['value'], True)

    def test_truncation_invalid_varints_utf8_and_partial_fields(self):
        schema = self.schema('message PeerInfoSubsriber {optional bool first=1; optional string text=2; optional uint64 big=3;}')
        for bad in (b'\x12\x05x', b'\x18' + b'\xff' * 10, b'\x00', b'\x0f'):
            with self.subTest(bad=bad):
                result = self.record(b'\x08\x01' + bad, schema)
                self.assertEqual(result.status, 'error')
                self.assertTrue(result.structure['data']['first'])
                self.assertIn('_unparsed_hex', result.structure['data'])
        result = self.record(b'\x08\x01\x12\x01\xff\x18\x05', schema)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.structure['data']['big'], 5)
        self.assertEqual(result.structure['data']['_unknown_fields'][0]['encoded_hex'], '1201FF')

    def test_recursion_and_special_float_safety(self):
        schema = self.schema('message PeerInfoSubsriber { optional PeerInfoSubsriber child=1; optional double number=2;}')
        nested = b''
        for _ in range(40): nested = blob(1, nested)
        self.assertEqual(self.record(nested, schema).status, 'error')
        result = self.record(b'\x11' + struct.pack('<d', float('nan')), schema)
        self.assertEqual(result.structure['data']['number'], 'NaN')
        json.dumps(result.structure, allow_nan=False)

    def test_malformed_input_never_dropped(self):
        schema = self.schema('message PeerInfoSubsriber {}')
        for text, kind in [('garbage', 'json'), ('{broken}', 'json'), ('44 54', 'frames'), ('0x4454', 'frames'), ('445', 'frames'), ('4454', 'frames'), ('{"id":1,"name":"up","sentence":"0Z"}', 'json'), ('{"id":true,"name":"up","sentence":""}', 'json')]:
            with self.subTest(text=text):
                result = decode_line(text, kind, schema)
                self.assertEqual(result.status, 'error')
                self.assertEqual(result.structure['source_text'], text)
                self.assertIn('data', result.structure)

    def test_import_scope_and_nested_enum(self):
        schema = self.schema('import "other.proto"; message PeerInfoSubsriber { optional pkg.Other child=1; }', extra=[('other', '// $(FILE)other.proto\nsyntax="proto2"; package pkg; message Other { enum E { Original=5; } optional E e=1; }')])
        self.assertEqual(self.record(blob(1, scalar(1, 5)), schema).structure['data'], {'child': {'e': 'Original'}})

    def test_explicit_document_roles_and_unmapped_protocol(self):
        schema = self.schema('message CustomRoot { optional bool flag=1; }\n// 设备端应答结构体：CustomRoot', opcode=10, title='new protocol')
        self.assertEqual(self.record(scalar(1, 1), schema, opcode=10).structure['message'], 'CustomRoot')
        schema = self.schema('message InnocentSubscriber {optional bool value=1;}', title='unknown protocol')
        self.assertIsNone(self.record(scalar(1, 1), schema).structure['message'])
        self.assertTrue(schema.warnings)

    def test_document_role_name_abuts_chinese_description(self):
        schema = self.schema('message RtSubscriber {optional bool value=1;} message RtNotification {optional string value=1;}\n// 服务端发送结构体：RtSubscriber获取实时数据；', title='realtime data')
        result = self.record(scalar(1, 1), schema, direction='down')
        self.assertEqual(result.structure['message'], 'RtSubscriber')
        self.assertEqual(result.status, 'decoded')

    def test_oneof_and_singular_message_merging(self):
        schema = self.schema('message PeerInfoSubsriber { message Child {optional uint32 a=1; optional uint32 b=2; repeated uint32 values=3;} optional Child child=1; oneof choice {uint32 a=2; uint32 b=3;} }')
        payload = blob(1, scalar(1, 1) + scalar(3, 7)) + blob(1, scalar(2, 2) + scalar(3, 8)) + scalar(2, 10) + scalar(3, 20)
        result = self.record(payload, schema)
        self.assertEqual(result.structure['data']['child'], {'a': 1, 'b': 2, 'values': [7, 8]})
        self.assertNotIn('a', result.structure['data'])
        self.assertEqual(result.structure['data']['b'], 20)
        self.assertIn('payload_hex', result.structure)
        self.assertEqual(result.status, 'warning')

    def test_unknown_groups_and_partial_packed_values(self):
        schema = self.schema('message PeerInfoSubsriber {optional bool value=2; repeated fixed32 nums=3;}')
        result = self.record(bytes.fromhex('530801541001'), schema)
        self.assertEqual(result.structure['data']['value'], True)
        self.assertEqual(result.structure['data']['_unknown_fields'][0]['encoded_hex'], '53080154')
        self.assertEqual(result.status, 'warning')
        self.assertEqual(self.record(bytes.fromhex('53080164'), schema).status, 'error')
        result = self.record(blob(3, struct.pack('<I', 123) + b'xx'), schema)
        self.assertEqual(result.structure['data']['nums'], [123])
        self.assertEqual(result.status, 'error')

    def test_escaped_surrogate_is_preserved_as_json_source(self):
        schema = self.schema('message PeerInfoSubsriber {}')
        source = json.dumps({'id': 1, 'name': 'up', 'sentence': chr(0xD800)})
        result = decode_line(source, 'json', schema)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.original_hex, source)
        result.original_hex.encode('utf-8')
        json.dumps(result.structure, ensure_ascii=False).encode('utf-8')

    def test_schema_cancel_and_invalid_declarations(self):
        class Cancel:
            def check(self): raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            load_protocol(self.root / 'missing', Cancel())
        with self.assertRaises(ProtoError):
            self.schema('message PeerInfoSubsriber { optional bool a=1; optional bool b=1; }')
        for name in ('_unknown_fields', '_unparsed_hex'):
            with self.assertRaisesRegex(ProtoError, '辅助字段冲突'):
                self.schema('message PeerInfoSubsriber { optional uint32 ' + name + '=1; }')


if __name__ == '__main__':
    unittest.main()
