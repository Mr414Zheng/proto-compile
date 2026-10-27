"""蓝牙日志解析任务：逐条保留结果，完整写出后再原子发布 TXT。"""

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ProtoError
from .ble_decoder import decode_line
from .ble_schema import load_protocol
from .service import CancellationToken, Cancelled, ConversionError

LOG_FORMATS = ('frames', 'json')
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_WARNINGS = 20


@dataclass
class BleDecodeRequest:
    protocol_path: str
    log_path: str
    log_format: str
    output_parent: str


@dataclass
class BleDecodeResult:
    output_path: str
    report_path: str
    record_count: int
    decoded_count: int
    warning_count: int
    error_count: int
    proto_count: int
    warnings: list = field(default_factory=list)
    source_files: list = field(default_factory=list)


def _input_file(value, label):
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ConversionError(f'找不到{label}：{path}', 'INPUT_NOT_FOUND',
                              f'请重新选择可读取的{label}。', file=str(path))
    return path


def decode_logs(request, event=None, cancellation=None):
    """读取选定协议与单个日志；坏记录进入报告，文件级异常不发布半成品。

    三类计数互斥，其和为 record_count。原始日志不包含的元数据由解码器
    明确标记为未知或推断，服务层不补造方向、时间或完整 BLE 帧。
    """
    token = cancellation or CancellationToken()

    def progress(stage, message, **counts):
        token.check()
        if event:
            event(stage=stage, message_zh=message, **counts)
        token.check()

    if request.log_format not in LOG_FORMATS:
        raise ConversionError('请选择完整 BLE 帧或格式化 JSON 日志。', 'INVALID_REQUEST')
    protocol = _input_file(request.protocol_path, '协议文档')
    log = _input_file(request.log_path, '蓝牙日志文件')
    parent = Path(request.output_parent)
    if not parent.is_absolute() or not parent.is_dir():
        raise ConversionError('导出目录不存在，请重新选择。', 'OUTPUT_NOT_FOUND',
                              '请选择一个已有且可写入的文件夹。', file=str(parent))
    progress('parsing', '正在读取协议文档并建立消息映射…')
    try:
        schema = load_protocol(protocol, cancellation=token)
    except Cancelled:
        raise
    except (ProtoError, OSError, UnicodeError, ValueError) as exc:
        raise ConversionError('无法解析所选协议文档。', 'BLE_PROTOCOL_INVALID',
                              '请选择包含 device protocol 定义的 TiddlyWiki HTML 文档，并检查错误详情。',
                              file=str(protocol), details=str(exc)) from exc
    warnings = list(schema.warnings[:MAX_WARNINGS])
    counts = dict(decoded=0, warning=0, error=0)
    record_count = 0
    last_decoding_progress = None
    name = re.sub(r'[\x00-\x1f/\\:]', '_', log.stem)[:80] or '蓝牙日志'
    report_name = f'{name}-解析.txt'
    destination = parent / f'蓝牙日志解析-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex}'
    progress('reading', '正在读取蓝牙日志…')
    phase = 'exporting'
    try:
        # 临时目录和最终目录位于同一文件系统；取消或 I/O 失败不暴露半份结果。
        with tempfile.TemporaryDirectory(prefix='.ble-decode-', dir=parent) as temporary:
            staged = Path(temporary) / 'result'
            staged.mkdir()
            with log.open('rb') as source, (staged / report_name).open('w', encoding='utf-8', newline='\n') as output:
                line_number = 0
                while True:
                    token.check()
                    phase = 'reading'
                    raw = source.readline(MAX_LINE_BYTES + 1)
                    if not raw:
                        break
                    line_number += 1
                    if len(raw) > MAX_LINE_BYTES:
                        raise ConversionError(f'日志第 {line_number} 行超过 8 MiB，无法安全解析。', 'BLE_LINE_TOO_LARGE',
                                              '请确认每行只有一条消息，并将过长记录拆分为正确的日志行。',
                                              file=str(log), line=line_number)
                    try:
                        line = raw.decode('utf-8-sig' if line_number == 1 else 'utf-8').rstrip('\r\n')
                    except UnicodeError as exc:
                        raise ConversionError(f'日志第 {line_number} 行不是有效的 UTF-8 文本。', 'BLE_LOG_ENCODING',
                                              '请将日志保存为 UTF-8 纯文本后重新导入。', file=str(log),
                                              line=line_number, details=str(exc)) from exc
                    if not line.strip():
                        continue
                    record = decode_line(line, request.log_format, schema)
                    if record.status not in counts:
                        raise RuntimeError('蓝牙解码器返回了未知状态。')
                    counts[record.status] += 1
                    record_count += 1
                    phase = 'exporting'
                    if record_count > 1:
                        output.write('\n\n')
                    output.write('原始十六进制字符串：\n')
                    output.write(record.original_hex)
                    output.write('\n\n结构体：\n')
                    output.write(json.dumps(record.structure, ensure_ascii=False, indent=2, allow_nan=False))
                    if record.status != 'decoded' and len(warnings) < MAX_WARNINGS:
                        warnings.append(f'第 {line_number} 行含有' + ('错误' if record.status == 'error' else '解析提示') + '，已保留在 TXT 结果中。')
                    now = time.monotonic()
                    if last_decoding_progress is None or now - last_decoding_progress >= 0.100:
                        progress('decoding', f'已解析 {record_count} 条记录…', processed=record_count)
                        last_decoding_progress = now
                if not record_count:
                    raise ConversionError('日志文件为空或只包含空行。', 'EMPTY_INPUT',
                                          '请选择包含蓝牙数据的日志文件。', file=str(log))
                phase = 'exporting'
                output.write('\n')
            progress('exporting', '正在保存解析结果…', processed=record_count, total=record_count)
            with token.publication():
                if destination.exists() or destination.is_symlink():
                    raise ConversionError('结果目录已被占用，未覆盖已有文件。', 'OUTPUT_EXISTS', '请重试以创建新的结果目录。')
                os.rename(staged, destination)
    except OSError as exc:
        # 打开输入和输出处于同一 with 声明，优先按异常的文件路径区分错误来源。
        reading = str(getattr(exc, 'filename', '')) == str(log) or (exc.filename is None and phase == 'reading')
        message = '无法读取蓝牙日志文件。' if reading else '无法导出蓝牙日志解析结果。'
        raise ConversionError(message, 'BLE_IO_FAILED',
                              '请检查文件访问权限、导出目录及剩余磁盘空间后重试。',
                              file=str(log if reading else parent), details=str(exc)) from exc
    return BleDecodeResult(str(destination), str(destination / report_name), record_count,
                           counts['decoded'], counts['warning'], counts['error'], schema.proto_count,
                           warnings, [str(protocol), str(log)])
