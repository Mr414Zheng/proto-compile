import binascii
import html
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from proto_tool.ble_service import BleDecodeRequest, decode_logs
from proto_tool.engine import main, parse_request
from proto_tool.service import CancellationToken, Cancelled, ConversionError


def protocol_document():
    items = {
        'Content': '* OPT_0001 [[peer information]]\n* OPT_FFFF [[connection params]]',
        'peer information': '''// $(FILE)peer_info.proto
syntax = "proto2";
message PeerInfoSubsriber { optional bool support_peer_type = 1; }
message PeerInfoNotification { optional bool connect_app = 10; }
''',
        'connection params': '''// $(FILE)con_params.proto
syntax = "proto2";
message ConParamsUpdate { optional fixed32 mtu = 1; }
''',
    }
    return '<html>' + ''.join(f'<div title="{title}"><pre>{html.escape(body)}</pre></div>'
                             for title, body in items.items()) + '</html>'


def json_line(payload='5001', direction='down'):
    return '2026-06-30 10:13:18:' + json.dumps(dict(id='1', name=direction, sentence=payload))


def full_frame(payload='5001', opcode=1):
    data = bytes.fromhex(payload)
    return (b'DT' + len(data).to_bytes(2, 'little') + binascii.crc_hqx(data, 0).to_bytes(2, 'little')
            + opcode.to_bytes(2, 'little') + data).hex().upper()


class BleServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='蓝牙日志测试 ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.protocol = self.root / 'protocol.html'
        self.protocol.write_text(protocol_document(), encoding='utf-8')
        self.log = self.root / 'messages.txt'
        self.log.write_text(json_line() + '\n', encoding='utf-8')
        self.output = self.root / 'output'
        self.output.mkdir()

    def request(self, format='json'):
        return BleDecodeRequest(str(self.protocol), str(self.log), format, str(self.output))

    def read_records(self, path):
        text = Path(path).read_text(encoding='utf-8')
        self.assertNotIn('raw_timestamp', text)
        parts = text.split('原始十六进制字符串：\n')[1:]
        return [(part.split('\n\n结构体：\n', 1)[0], json.loads(part.split('\n\n结构体：\n', 1)[1])) for part in parts]

    def test_json_report_preserves_order_duplicates_and_explicit_values(self):
        self.log.write_text('\ufeff' + json_line() + '\r\n\n' + json_line('5000') + '\n' + json_line(), encoding='utf-8')
        result = decode_logs(self.request())
        self.assertEqual((result.record_count, result.decoded_count, result.warning_count, result.error_count), (3, 3, 0, 0))
        records = self.read_records(result.report_path)
        self.assertEqual([raw for raw, _ in records], ['5001', '5000', '5001'])
        self.assertEqual([item['data'] for _, item in records], [{'connect_app': True}, {'connect_app': False}, {'connect_app': True}])
        self.assertTrue(all(item['timestamp'] == '2026-06-30 10:13:18' for _, item in records))
        self.assertEqual([p.name for p in Path(result.output_path).iterdir()], ['messages-解析.txt'])

    def test_frames_and_corrupt_records_are_retained(self):
        bad = full_frame()[:8] + '0000' + full_frame()[12:]
        self.log.write_text(full_frame() + '\n' + bad + '\ninvalid-hex\n', encoding='utf-8')
        result = decode_logs(self.request('frames'))
        records = self.read_records(result.report_path)
        self.assertEqual(result.record_count, 3)
        self.assertEqual(result.record_count, result.decoded_count + result.warning_count + result.error_count)
        self.assertGreaterEqual(result.error_count, 1)
        self.assertEqual(records[0][1]['message'], 'PeerInfoNotification')
        self.assertEqual(records[0][1]['direction'], 'down')
        self.assertIsNone(records[0][1]['timestamp'])
        self.assertEqual(records[2][0], 'invalid-hex')

    def test_unique_directories_never_overwrite_existing_reports(self):
        first = decode_logs(self.request())
        saved = Path(first.report_path).read_bytes()
        second = decode_logs(self.request())
        self.assertNotEqual(first.output_path, second.output_path)
        self.assertEqual(saved, Path(first.report_path).read_bytes())
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_cancel_before_publication_cleans_staging(self):
        token = CancellationToken()
        def event(**fields):
            if fields['stage'] == 'exporting':
                token.cancel()
        with self.assertRaises(Cancelled):
            decode_logs(self.request(), event=event, cancellation=token)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_empty_encoding_and_oversize_fail_without_output(self):
        for content, code in [(b'\n \n', 'EMPTY_INPUT'), (b'\xff\xfe', 'BLE_LOG_ENCODING')]:
            self.log.write_bytes(content)
            with self.subTest(code=code), self.assertRaises(ConversionError) as caught:
                decode_logs(self.request())
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(list(self.output.iterdir()), [])
        self.log.write_text(json_line(), encoding='utf-8')
        with patch('proto_tool.ble_service.MAX_LINE_BYTES', 8), self.assertRaises(ConversionError) as caught:
            decode_logs(self.request())
        self.assertEqual(caught.exception.code, 'BLE_LINE_TOO_LARGE')
        self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_source_and_publication_failure_are_chinese(self):
        self.log.unlink()
        with self.assertRaises(ConversionError) as caught:
            decode_logs(self.request())
        self.assertIn('蓝牙日志文件', str(caught.exception))
        self.log.write_text(json_line(), encoding='utf-8')
        with patch('proto_tool.ble_service.os.rename', side_effect=PermissionError('denied')), self.assertRaises(ConversionError) as caught:
            decode_logs(self.request())
        self.assertIn('无法导出', str(caught.exception))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_large_log_streaming_and_bounded_diagnostics(self):
        self.log.write_text((json_line() + '\n') * 7044 + 'bad\n' * 30, encoding='utf-8')
        result = decode_logs(self.request())
        self.assertEqual((result.record_count, result.decoded_count, result.error_count), (7074, 7044, 30))
        self.assertLessEqual(len(result.warnings), 20)
        self.assertEqual(len(self.read_records(result.report_path)), 7074)

    def engine_request(self):
        return dict(protocol_version=1, task_id='ble-test', action='decode_ble', input_kind='html',
                    input_paths=[str(self.protocol)], include_paths=[], output_parent=str(self.output),
                    log_path=str(self.log), log_format='json')

    def test_engine_request_and_real_result(self):
        data = self.engine_request()
        self.assertIsInstance(parse_request(data), BleDecodeRequest)
        out = io.StringIO()
        self.assertEqual(main(io.StringIO(json.dumps(data) + '\n'), out), 0)
        messages = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(messages[-1]['record_count'], 1)
        self.assertEqual(messages[-1]['type'], 'result')
        self.assertTrue(Path(messages[-1]['report_path']).is_file())
        self.assertTrue(all(message['task_id'] == 'ble-test' for message in messages))

    def test_invalid_engine_request_is_not_converted(self):
        for key, value in [('log_path', 'relative.txt'), ('log_format', 'other'), ('input_paths', []), ('input_kind', 'proto'), ('output_parent', None)]:
            request = self.engine_request()
            request[key] = value
            with self.subTest(key=key), self.assertRaises(ConversionError):
                parse_request(request)


if __name__ == '__main__':
    unittest.main()
