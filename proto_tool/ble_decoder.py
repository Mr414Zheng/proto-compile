"""保留来源及部分结果的 BLE 单行解码器。"""

import binascii
import json
import math
import re
import struct
from dataclasses import dataclass, field

from .ble_schema import ProtocolSchema, Type

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_FIELDS = 100000
_HEX = re.compile(r'[0-9a-fA-F]*\Z')
_VARINT = {'int32', 'int64', 'uint32', 'uint64', 'sint32', 'sint64', 'bool'}
_FIXED32 = {'fixed32', 'sfixed32', 'float'}
_FIXED64 = {'fixed64', 'sfixed64', 'double'}


@dataclass
class DecodedRecord:
    """每个输入行恰好对应一条记录；原始内容与展示结构不互相替代。"""
    original_hex: str
    structure: dict
    status: str


@dataclass
class _Decoded:
    data: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    matched: int = 0
    mismatches: int = 0
    unknown: int = 0


class _WireError(ValueError):
    pass


def crc16(payload: bytes) -> int:
    """协议 CRC-16：初值 0，多项式 0x1021，覆盖 payload。"""
    return binascii.crc_hqx(payload, 0)


def _varint(payload, offset):
    value = 0
    for index in range(10):
        if offset >= len(payload): raise _WireError('varint 数据截断')
        byte = payload[offset]; offset += 1
        if index == 9 and byte > 1: raise _WireError('varint 超出 64 位范围')
        value |= (byte & 0x7F) << (index * 7)
        if byte < 0x80: return value, offset
    raise _WireError('varint 编码超过 10 字节')


def _read(payload, offset, wire):
    if wire == 0: return _varint(payload, offset)
    if wire == 2:
        size, offset = _varint(payload, offset)
    elif wire in (1, 5): size = 8 if wire == 1 else 4
    else: raise _WireError(f'不支持或无效的 wire type {wire}')
    if size > len(payload) - offset: raise _WireError(f'字段长度 {size} 超出剩余 {len(payload) - offset} 字节')
    return payload[offset:offset + size], offset + size


def _group(payload, offset, field_number, depth, budget):
    if depth > MAX_DEPTH: raise _WireError('未知 group 嵌套超过安全限制')
    start = offset
    while offset < len(payload):
        budget[0] -= 1
        if budget[0] < 0: raise _WireError('字段数量超过安全限制')
        tag, offset = _varint(payload, offset)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < 2 ** 29: raise _WireError('group 字段编号无效')
        if wire == 4:
            if number != field_number: raise _WireError('group 结束标签不匹配')
            return payload[start:offset], offset
        if wire == 3:
            _, offset = _group(payload, offset, number, depth + 1, budget)
        else:
            _, offset = _read(payload, offset, wire)
    raise _WireError('group 缺少结束标签')


def _wire_for(resolved):
    if isinstance(resolved, Type): return 2 if resolved.kind == 'message' else 0
    if resolved in _VARINT: return 0
    if resolved in _FIXED32: return 5
    if resolved in _FIXED64: return 1
    return 2


def _signed(value, bits):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _scalar(value, resolved, result, label):
    if isinstance(resolved, Type):
        number = _signed(value, 32)
        if number not in resolved.values:
            result.warnings.append(f'{label}: 未知枚举值 {number}，保留数值及原始编码')
        return resolved.values.get(number, number)
    if resolved == 'bool':
        if value not in (0, 1): result.warnings.append(f'{label}: bool 使用非标准数值 {value}，按非零为真解释并保留原始编码')
        return bool(value)
    if resolved == 'bytes': return value.hex().upper()
    if resolved == 'string':
        try: return value.decode('utf-8')
        except UnicodeDecodeError as exc: raise _WireError('字符串不是有效 UTF-8') from exc
    if resolved in ('float', 'double'):
        number = struct.unpack('<f' if resolved == 'float' else '<d', value)[0]
        if not math.isfinite(number):
            result.warnings.append(f'{label}: 特殊浮点值以字符串保留，并记录原始编码')
            return 'NaN' if math.isnan(number) else ('Infinity' if number > 0 else '-Infinity')
        return number
    if resolved in ('uint32', 'sint32') and value > 0xFFFFFFFF:
        result.warnings.append(f'{label}: varint 超出 32 位，按 Protobuf 规则取低位并保留原始编码')
    if resolved == 'int32' and 0xFFFFFFFF < value < 0xFFFFFFFF80000000:
        result.warnings.append(f'{label}: int32 非标准扩展编码，保留原始编码供复核')
    if resolved in _FIXED32 | _FIXED64:
        number = int.from_bytes(value, 'little', signed=resolved.startswith('s'))
    elif resolved.startswith('sint'):
        bits = 32 if resolved.endswith('32') else 64
        value &= (1 << bits) - 1
        number = (value >> 1) ^ -(value & 1)
    elif resolved.startswith('int'):
        number = _signed(value, 32 if resolved.endswith('32') else 64)
    elif resolved == 'uint32': number = value & 0xFFFFFFFF
    else: number = value
    if abs(number) > 2 ** 53 - 1:
        result.warnings.append(f'{label}: 超过 JSON 常用安全整数范围，以十进制字符串无损保留')
        return str(number)
    return number


def _unknown(result, number, wire, encoded):
    result.data.setdefault('_unknown_fields', []).append({'field_number': number, 'wire_type': wire, 'encoded_hex': encoded.hex().upper()})


def _store(data, f, value, message):
    if f.oneof:
        for other in message.fields.values():
            if other.oneof == f.oneof and other.name != f.name:
                data.pop(other.name, None)
    if f.repeated:
        data.setdefault(f.name, []).extend(value)
    elif f.name in data and isinstance(f.resolved, Type) and f.resolved.kind == 'message':
        fields = {item.name: item for item in f.resolved.fields.values()}
        for name, item in value.items():
            if name in fields:
                _store(data[f.name], fields[name], item, f.resolved)
            elif name == '_unknown_fields':
                data[f.name].setdefault(name, []).extend(item)
            else:
                data[f.name][name] = item
    else:
        data[f.name] = value


def _decode(payload, message, depth=0, budget=None):
    result = _Decoded()
    if depth > MAX_DEPTH:
        result.errors.append(f'嵌套消息超过 {MAX_DEPTH} 层限制')
        result.data['_unparsed_hex'] = payload.hex().upper()
        return result
    budget = [MAX_FIELDS] if budget is None else budget
    offset = 0
    seen = set()
    while offset < len(payload):
        start = offset
        number, wire = None, None
        try:
            budget[0] -= 1
            if budget[0] < 0: raise _WireError('字段数量超过安全限制')
            tag, offset = _varint(payload, offset)
            number, wire = tag >> 3, tag & 7
            if not 0 < number < 2 ** 29: raise _WireError('字段编号无效')
            value, offset = _group(payload, offset, number, depth + 1, budget) if wire == 3 else _read(payload, offset, wire)
        except _WireError as exc:
            result.errors.append(f'偏移 {start}: {exc}')
            result.data['_unparsed_hex'] = payload[start:].hex().upper()
            break
        f = message.fields.get(number) if message else None
        if f is None or f.resolved is None:
            _unknown(result, number, wire, payload[start:offset])
            result.unknown += 1
            result.warnings.append(f'字段 {number}: 未知字段或类型，保留原始编码')
            continue
        expected = _wire_for(f.resolved)
        packed = f.repeated and wire == 2 and expected in (0, 1, 5)
        if wire != expected and not packed:
            result.mismatches += 1
            _unknown(result, number, wire, payload[start:offset])
            result.warnings.append(f'{f.name}: wire type {wire} 与协议期望 {expected} 不兼容')
            continue
        before_warnings = len(result.warnings)
        try:
            if packed:
                values, inner = [], 0
                while inner < len(value):
                    budget[0] -= 1
                    if budget[0] < 0: raise _WireError('packed 元素数量超过安全限制')
                    item, inner = _read(value, inner, expected)
                    values.append(_scalar(item, f.resolved, result, f.name))
            elif isinstance(f.resolved, Type) and f.resolved.kind == 'message':
                child = _decode(value, f.resolved, depth + 1, budget)
                values = [child.data]
                result.warnings.extend(f'{f.name}.{w}' for w in child.warnings)
                result.errors.extend(f'{f.name}.{e}' for e in child.errors)
            else:
                values = [_scalar(value, f.resolved, result, f.name)]
            if f.name in result.data and not f.repeated:
                result.warnings.append(f'{f.name}: 单值字段重复编码，消息按 Protobuf 合并、标量取最后值；完整 payload 已保留')
            if f.oneof and any(other.oneof == f.oneof and other.name != f.name and other.name in result.data for other in message.fields.values()):
                result.warnings.append(f'{f.oneof}: 多个 oneof 成员编码，按最后成员展示；完整 payload 已保留')
            _store(result.data, f, values if f.repeated else values[0], message)
            if number not in seen:
                result.matched += 1; seen.add(number)
            if len(result.warnings) > before_warnings and not (isinstance(f.resolved, Type) and f.resolved.kind == 'message'):
                _unknown(result, number, wire, payload[start:offset])
        except _WireError as exc:
            if packed and values:
                result.data.setdefault(f.name, []).extend(values)
            result.errors.append(f'偏移 {start}, {f.name}: {exc}')
            _unknown(result, number, wire, payload[start:offset])
    return result


def decode_line(line: str, log_format: str, schema: ProtocolSchema) -> DecodedRecord:
    """解析纯帧或时间前缀 JSON；损坏记录返回错误结果，绝不丢弃输入行。"""
    text = line.rstrip('\r\n')
    original = text
    envelope = {'timestamp': None, 'opcode': None, 'direction': None, 'message': None, 'data': {}}
    errors, warnings = [], []
    payload = None
    opcode = None
    try:
        if log_format == 'json':
            start = text.find('{')
            if start < 0: raise ValueError('未找到 JSON 对象')
            prefix = text[:start]
            if prefix.strip():
                if not prefix.endswith(':'): raise ValueError('时间与 JSON 之间缺少冒号')
                envelope['timestamp'] = prefix[:-1]
            obj = json.loads(text[start:])
            if not isinstance(obj, dict): raise ValueError('JSON 记录必须是对象')
            sentence = obj.get('sentence')
            if not isinstance(sentence, str): raise ValueError('sentence 必须是十六进制字符串')
            try:
                sentence.encode('utf-8')
            except UnicodeEncodeError as exc:
                raise ValueError('sentence 含无法写入 UTF-8 的孤立代理字符，已保留原始 JSON 文本') from exc
            original = sentence
            raw_id = obj.get('id')
            if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)) or not re.fullmatch(r'[0-9]+', str(raw_id)):
                raise ValueError('id 必须是十进制操作码字符串或整数')
            opcode = int(raw_id)
            if not 0 <= opcode <= 0xFFFF: raise ValueError('操作码超出 0..65535 范围')
            envelope['opcode'] = f'0x{opcode:04X}'
            if obj.get('name') not in ('up', 'down'): raise ValueError('name 必须是 up 或 down')
            envelope['direction'] = obj['name']
            if len(sentence) % 2 or not _HEX.fullmatch(sentence): raise ValueError('sentence 必须是偶数个十六进制字符，不能含空格或 0x 前缀')
            if len(sentence) > MAX_PAYLOAD_BYTES * 2: raise ValueError('payload 超过安全大小限制')
            payload = bytes.fromhex(sentence)
        elif log_format == 'frames':
            if not text or len(text) % 2 or not _HEX.fullmatch(text): raise ValueError('完整帧必须是偶数个十六进制字符，不能含空格或 0x 前缀')
            if len(text) > (MAX_PAYLOAD_BYTES + 8) * 2: raise ValueError('完整帧超过安全大小限制')
            frame = bytes.fromhex(text)
            if len(frame) < 8: raise ValueError('完整帧不足 8 字节帧头')
            size, expected_crc, opcode = struct.unpack('<HHH', frame[2:8])
            payload = frame[8:]
            envelope['opcode'] = f'0x{opcode:04X}'
            actual_crc = crc16(payload)
            envelope['frame_validation'] = {'prefix_valid': frame[:2] == b'DT', 'declared_length': size, 'actual_length': len(payload), 'length_valid': len(payload) == size, 'expected_crc': f'0x{expected_crc:04X}', 'actual_crc': f'0x{actual_crc:04X}', 'crc_valid': actual_crc == expected_crc}
            if frame[:2] != b'DT': errors.append('帧前缀不是 DT (4454)')
            if len(payload) != size: errors.append('帧 payload 长度与帧头 len 不一致')
            if actual_crc != expected_crc: errors.append('payload CRC-16 校验失败')
        else:
            raise ValueError('未知日志格式: ' + str(log_format))
    except (ValueError, TypeError, RecursionError) as exc:
        errors.append('输入记录无效: ' + str(exc))
        envelope['source_text'] = text
    if payload is not None:
        candidates = schema.candidates.get(opcode, [])
        if log_format == 'json': candidates = [c for c in candidates if c.direction == envelope['direction']]
        results = [(c, _decode(payload, c.message)) for c in candidates]
        chosen = None
        if len(results) == 1:
            chosen = results[0]
        elif results:
            # 只使用已实际编码字段；缺少 required 不能惩罚旧版设备。
            ranked = sorted(results, key=lambda pair: (not pair[1].errors, -pair[1].mismatches, pair[1].matched, -pair[1].unknown), reverse=True)
            first = ranked[0][1]
            key = lambda r: (not r.errors, -r.mismatches, r.matched, -r.unknown)
            if first.matched > 0 and first.mismatches == 0 and not first.errors and key(first) > key(ranked[1][1]):
                chosen = ranked[0]
            elif all(c.message is results[0][0].message for c, _ in results):
                chosen = results[0]
        if chosen:
            candidate, decoded = chosen
            envelope['message'] = candidate.message.name
            envelope['data'] = decoded.data
            warnings.extend(decoded.warnings); errors.extend(decoded.errors)
            if log_format == 'frames':
                same_message_directions = {c.direction for c, _ in results if c.message is candidate.message}
                envelope['direction'] = candidate.direction if len(same_message_directions) == 1 else None
                envelope['interpretation'] = {'inferred': True, 'basis': '依据当前文档操作码映射及实际字段编号和 wire type 比较候选；纯帧未记录方向', 'candidates': [{'direction': c.direction, 'message': c.message.name, 'matched_fields': d.matched, 'wire_mismatches': d.mismatches, 'errors': d.errors[:8]} for c, d in results]}
                if envelope['direction'] is None: warnings.append('双向使用相同消息，无法确定传输方向')
                else: warnings.append('方向及消息为协议和 payload 推断结果，并非输入记录事实')
        else:
            decoded = _decode(payload, None)
            envelope['data'] = decoded.data
            errors.extend(decoded.errors)
            warnings.append('多个候选无法可靠区分' if results else '当前文档没有该操作码及方向的可用根消息映射')
            envelope['interpretation'] = {'inferred': False, 'candidates': [{'direction': c.direction, 'message': c.message.name, 'data': d.data, 'matched_fields': d.matched, 'wire_mismatches': d.mismatches, 'errors': d.errors[:8]} for c, d in results]}
        if errors or warnings: envelope['payload_hex'] = payload.hex().upper()
    status = 'error' if errors else ('warning' if warnings else 'decoded')
    envelope['parse_status'] = status
    if errors: envelope['parse_error'] = list(dict.fromkeys(errors))[:100]
    if warnings: envelope['parse_warnings'] = list(dict.fromkeys(warnings))[:100]
    return DecodedRecord(original, envelope, status)
