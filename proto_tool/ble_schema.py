"""从选中的协议文档读取原始名称及 BLE 根消息映射，不执行代码生成改名。"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import ProtoError
from .extract import WikiParser, extract, READ_CHUNK_SIZE, safe_name
from .normalize import Schema, SCALARS


@dataclass
class Field:
    name: str
    number: int
    type_name: str
    repeated: bool = False
    resolved: object = None
    oneof: str = None


@dataclass
class Type:
    name: str
    scope: tuple
    kind: str
    fields: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)
    source_name: str = ''

    @property
    def path(self):
        return self.scope + (self.name,)


@dataclass
class Candidate:
    direction: str
    message: Type


@dataclass
class ProtocolSchema:
    """文件作用域内解析的类型与文档操作码映射；无法解释的类型保留为未解析字段。"""
    proto_count: int
    warnings: list
    candidates: dict


class _Parser(Schema):
    # 复用既有词法器、括号处理与错误位置，只收集解码所需的原始声明。
    def parse_scope(self, scope, closing=False, oneof=False):
        if len(scope) > 64:
            self.fail('协议类型嵌套超过 64 层')
        while self.peek() and self.peek() != '}':
            kind = self.peek()
            if kind == ';':
                self.pop()
            elif kind == 'syntax' and not scope:
                self.pop(); self.pop('=')
                if self.pop().value not in ('"proto2"', '"proto3"', "'proto2'", "'proto3'"):
                    self.fail('不支持的 syntax')
                self.syntax_end = self.pop(';').end
            elif kind == 'package' and not scope:
                self.pop(); self.package = self.qualified()[0]; self.pop(';')
            elif kind == 'import' and not scope:
                self.pop()
                modifier = self.pop().value if self.peek() in ('weak', 'public') else ''
                value = self.pop().value
                if not value.startswith(('"', "'")) or '\\' in value:
                    self.fail('import 必须是未转义的字符串路径')
                self.imports.append((safe_name(value[1:-1]), modifier)); self.pop(';')
            elif kind in ('message', 'enum') and not oneof:
                self.pop()
                symbol = Type(self.identifier().value, scope, kind, source_name=self.source.name)
                self.add_symbol(symbol); self.pop('{')
                if kind == 'message':
                    self.parse_scope(symbol.path, closing=True)
                else:
                    while self.peek() and self.peek() != '}':
                        if self.peek() in ('option', 'reserved', ';'):
                            self.statement()
                        else:
                            name = self.identifier().value; self.pop('=')
                            sign = -1 if self.peek() == '-' else 1
                            if sign == -1: self.pop()
                            number = self._number() * sign
                            symbol.values.setdefault(number, name)
                            self.statement()
                    self.pop('}')
            elif kind == 'oneof' and scope and not oneof:
                self.pop(); group = self.identifier().value; self.pop('{')
                self.parse_scope(scope, closing=True, oneof=group)
            elif kind in ('option', 'reserved', 'extensions'):
                self.statement()
            elif scope:
                repeated = False
                if kind in ('optional', 'required', 'repeated'):
                    repeated = self.pop().value == 'repeated'
                if self.peek() in ('group', 'map', 'extend'):
                    self.fail('日志解码暂不支持 group、map 或 extend 声明')
                type_name = self.qualified()[0]
                name = self.identifier().value; self.pop('=')
                if name in ('_unknown_fields', '_unparsed_hex'):
                    self.fail('字段名与解码辅助字段冲突，暂不支持: ' + name)
                number = self._number(); self.statement()
                owner = self.symbols[scope]
                if not 0 < number < 2 ** 29 or number in owner.fields:
                    self.fail('字段编号无效或重复: ' + str(number))
                if any(f.name == name for f in owner.fields.values()):
                    self.fail('字段名称重复: ' + name)
                owner.fields[number] = Field(name, number, type_name, repeated, oneof=oneof or None)
            else:
                self.fail('日志解码不支持声明 ' + repr(kind))
        if closing: self.pop('}')
        elif self.peek(): self.fail('多余的右括号')

    def _number(self):
        value = self.pop().value
        if not value.isdecimal(): self.fail('字段或枚举编号必须是整数')
        if value == '0' and self.peek().lower().startswith('x'):
            try: return int(value + self.pop().value, 16)
            except ValueError: self.fail('十六进制编号无效')
        try:
            return int(value, 8) if len(value) > 1 and value.startswith('0') else int(value)
        except ValueError:
            self.fail('整数编号无效或过长')


# 这些是协议族的根消息角色约定；操作码数值仍完全来自当前文档 Content 链接。
# 不按 Subscriber/Notification 后缀猜方向：实时数据和配置类协议的角色恰好相反。
_ROLES = {
    'connection params': ('ConParamsUpdate', 'ConParamsUpdate'),
    'device information': ('DeviceInfoResponse', 'DeviceInfoRequest'),
    'peer information': ('PeerInfoSubsriber PeerInfoSubscriber', 'PeerInfoNotification'),
    'msg notify': ('MsgSubscriber', 'MsgNotification'),
    'weather': ('WeatherSubscriber', 'WeatherNotification'),
    'alarmclock': ('AlarmSubscriber', 'AlarmNotification'),
    'sedentariness': ('SedtSubscriber', 'SedtNotification'),
    'device configuration': ('DeviceConfSubscriber', 'DeviceConfNotification'),
    'calendar': ('CalendarSubscriber', 'CalendarNotification'),
    'motor configuration': ('MotorConfSubscriber', 'MotorConfNotification'),
    'data information': ('DataInfoResponse', 'DataInfoRequest'),
    'om0_command': ('OM0Response OM0Report', 'OM0Command'),
    'shbus_command': ('SBResponse', 'SBCommand'),
    'c110_command': ('C110Response', 'C110Command'),
    'airquality': ('AirQualityConfirm', 'AirQualityNotification'),
    'custom dial': ('CustomDialConfim', 'CustomDialSubscriber'),
    'factory test': ('FactoryTestConfim', 'FactoryTestNotify'),
    'phone book': ('PhoneBookConfirm', 'PhoneBookNotify'),
    'realtime data': ('RtNotification', 'RtSubscriber'),
    'history data': ('HisNotification', 'HisSubscriber'),
    'files update': ('FilesUpdateResponse', 'FilesUpdateRequest'),
    'debug configuration': ('dbgConfSubscriber', 'dbgConfNotification'),
    'device configuration Switch': ('DeviceConfSwitchSubscriber', 'DeviceConfSwitchNotification'),
}


def load_protocol(path: Path, cancellation=None) -> ProtocolSchema:
    """读取所选 HTML；取消异常原样传播，文档结构错误抛出中文 ProtoError。"""
    def check():
        if cancellation is not None:
            cancellation.check() if hasattr(cancellation, 'check') else cancellation()
    path = Path(path)
    check()
    sources = extract(path)
    check()
    parsers, warnings = {}, []
    for name, source in sources.items():
        check()
        if name.endswith('.proto'):
            parsers[name] = _Parser(source, output_language='objc')
    for name, parser in parsers.items():
        check()
        visible = {name}
        pending = [dep for dep, _ in parser.imports]
        while pending:
            dep = pending.pop()
            if dep in visible: continue
            visible.add(dep)
            if dep not in parsers:
                warnings.append(f'{name}: 缺少导入 {dep}，相关字段保留原始编码')
                continue
            pending.extend(child for child, modifier in parsers[dep].imports if modifier == 'public')
        for symbol in parser.symbols.values():
            for f in symbol.fields.values():
                if f.type_name in SCALARS:
                    f.resolved = f.type_name
                    continue
                parts = tuple(f.type_name.lstrip('.').split('.'))
                lexical = tuple(filter(None, parser.package.split('.'))) + symbol.path
                prefixes = [()] if f.type_name.startswith('.') else [lexical[:i] for i in range(len(lexical), -1, -1)]
                for prefix in prefixes:
                    matches = []
                    for dep in visible:
                        if dep not in parsers: continue
                        other = parsers[dep]
                        package = tuple(filter(None, other.package.split('.')))
                        full = prefix + parts
                        if full[:len(package)] == package:
                            found = other.symbols.get(full[len(package):])
                            if found is not None: matches.append(found)
                    if matches:
                        if len(matches) == 1: f.resolved = matches[0]
                        break
                if f.resolved is None:
                    warnings.append(f'{name}: {symbol.name}.{f.name} 类型 {f.type_name} 无法唯一解析，保留原始编码')
    bodies = {}
    reader = WikiParser(on_item=lambda title, body: bodies.setdefault(title, body) if not title.startswith('$:/') else None)
    with path.open(encoding='utf-8-sig') as handle:
        while chunk := handle.read(READ_CHUNK_SIZE):
            check(); reader.feed(chunk)
    reader.close()
    candidates = {}
    # 只接受每行的主 OPT 链接，避免把 NOTE 或相关协议链接误当成本操作码。
    for match in re.finditer(r'(?mi)^\s*\*\s*OPT_([0-9a-f]{4})\s+\[\[([^\]]+)\]\]', bodies.get('Content', '')):
        opcode = int(match[1], 16)
        title = match[2].split('|')[-1]
        roots = [s for p in parsers.values() if title in p.source.titles for s in p.symbols.values() if not s.scope and s.kind == 'message']
        roles = {direction: names.split() for direction, names in zip(('up', 'down'), _ROLES.get(title, ('', '')))}
        # 文档明确写出的角色可扩展新协议；不从含糊的正文提及中推断。
        linked = bodies.get(title, '') + '\n' + '\n'.join(bodies.get(link.split('|')[-1], '') for link in re.findall(r'\[\[([^\]]+)\]\]', match.string[match.end():].split('\n', 1)[0]))
        for direction, pattern in [('down', r'服务端发送结构体\s*[：:]\s*([A-Za-z_][A-Za-z0-9_]*)'), ('up', r'设备端应答结构体\s*[：:]\s*([A-Za-z_][A-Za-z0-9_]*)')]:
            explicit = re.findall(pattern, linked)
            if explicit and all(any(root.name == name for root in roots) for name in explicit):
                roles[direction] = explicit
        mapped = candidates.setdefault(opcode, [])
        for direction, names in roles.items():
            for root in roots:
                if root.name in names and not any(c.direction == direction and c.message is root for c in mapped):
                    mapped.append(Candidate(direction, root))
        if not mapped:
            warnings.append(f'0x{opcode:04X} ({title}) 没有可确认的根消息映射')
    if not candidates:
        warnings.append('Content 未提供可识别的 OPT_xxxx 主链接，无法确定操作码消息映射')
    return ProtocolSchema(len(parsers), warnings, candidates)
