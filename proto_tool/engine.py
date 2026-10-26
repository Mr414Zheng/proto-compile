"""桌面应用使用的版本化单请求 NDJSON 进程接口。"""

import json
import signal
import sys
import traceback
from dataclasses import asdict
from pathlib import Path

from . import ProtoError
from .service import CancellationToken, ConversionError, ConversionRequest, OUTPUT_LANGUAGES, convert
from .toolchain import RUNTIME_VERSION, TARGET_RUNTIMES

PROTOCOL_VERSION = 1


def parse_request(data):
    if not isinstance(data, dict):
        raise ConversionError('请求必须是 JSON 对象。', 'INVALID_REQUEST')
    if type(data.get('protocol_version')) is not int or data['protocol_version'] != PROTOCOL_VERSION:
        raise ConversionError('进程协议版本不受支持，请更新应用。', 'UNSUPPORTED_PROTOCOL')
    if not isinstance(data.get('task_id'), str) or not data['task_id'].strip():
        raise ConversionError('请求缺少有效任务编号。', 'INVALID_REQUEST')
    if data.get('action') not in ('inspect', 'convert', 'version'):
        raise ConversionError('请求操作无效。', 'INVALID_REQUEST')
    if data.get('input_kind') not in ('html', 'proto'):
        raise ConversionError('输入类型必须为 HTML 或 proto。', 'INVALID_REQUEST')
    for key in ('input_paths', 'include_paths'):
        if not isinstance(data.get(key), list) or not all(isinstance(p, str) and Path(p).is_absolute() for p in data[key]):
            raise ConversionError(f'{key} 必须是绝对路径数组。', 'INVALID_REQUEST')
    target_runtime = data.get('target_runtime', RUNTIME_VERSION)
    if target_runtime not in TARGET_RUNTIMES:
        raise ConversionError('不支持的运行库版本，请选择 3.25.9 或 3.6.1。', 'INVALID_REQUEST')
    output_language = data.get('output_language', 'java')
    if output_language not in OUTPUT_LANGUAGES:
        raise ConversionError('输出语言必须为 Java 或 Objective-C。', 'INVALID_REQUEST')
    parent = data.get('output_parent')
    if parent is not None and (not isinstance(parent, str) or not Path(parent).is_absolute()):
        raise ConversionError('输出父目录必须是绝对路径。', 'INVALID_REQUEST')
    for name, action in (('preparation_output', 'inspect'), ('preparation_path', 'convert')):
        value = data.get(name)
        if value is not None and (not isinstance(value, str) or not Path(value).is_absolute() or data['action'] != action):
            raise ConversionError(f'{name} 仅可用于 {action}，且必须为绝对路径。', 'INVALID_REQUEST')
    if type(data.get('exclude_opt_0081', True)) is not bool:
        raise ConversionError('OPT_0081 排除开关必须为布尔值。', 'INVALID_REQUEST')
    if data['input_kind'] == 'html' and data['include_paths']:
        raise ConversionError('HTML 模式从文档内解析依赖，不能额外指定 include 目录。', 'INVALID_REQUEST')
    return ConversionRequest(input_kind=data['input_kind'], input_paths=data['input_paths'],
                             include_paths=data['include_paths'], output_parent=parent,
                             action=data['action'], exclude_opt_0081=data.get('exclude_opt_0081', True),
                             target_runtime=target_runtime, output_language=output_language, preparation_output=data.get('preparation_output'),
                             preparation_path=data.get('preparation_path'))


def error_fields(exc):
    if isinstance(exc, ConversionError):
        result = dict(code=exc.code, message_zh=str(exc), suggestion=exc.suggestion, details=exc.details)
        if exc.file is not None:
            result['file'] = exc.file
        if exc.line is not None:
            result['line'] = exc.line
        return result
    if isinstance(exc, (ProtoError, OSError, UnicodeError)):
        return dict(code='CONVERSION_FAILED', message_zh='转换失败：' + str(exc),
                    suggestion='请检查输入文件、访问权限、依赖目录和错误详情后重试。', details=str(exc))
    return dict(code='INTERNAL_ERROR', message_zh='转换引擎发生内部错误。',
                suggestion='请保留错误详情并重新启动应用；若仍失败，请联系维护者。', details=traceback.format_exc())


def main(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    token = CancellationToken()
    previous = signal.signal(signal.SIGTERM, lambda *_: token.cancel())
    task_id = ''

    def emit(type, **fields):
        stdout.write(json.dumps(dict(protocol_version=PROTOCOL_VERSION, task_id=task_id, type=type, **fields), ensure_ascii=False) + '\n')
        stdout.flush()

    try:
        line = stdin.readline()
        try:
            data = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise ConversionError('请求不是有效的 JSON，请重新发起转换。', 'INVALID_JSON', details=str(exc)) from exc
        if isinstance(data, dict) and isinstance(data.get('task_id'), str):
            task_id = data['task_id']
        request = parse_request(data)
        result = convert(request, event=lambda **fields: emit('event', **fields), cancellation=token)
        emit('result', **asdict(result))
        return 0
    except Exception as exc:
        emit('error', **error_fields(exc))
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
