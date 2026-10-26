import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import ProtoError
from .extract import Source, safe_name


SCALARS = set('double float int32 int64 uint32 uint64 sint32 sint64 fixed32 fixed64 sfixed32 sfixed64 bool string bytes'.split())
JAVA_RESERVED = set('abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for goto if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this throw throws transient try void volatile while true false null _'.split())
TOKEN = re.compile(r'\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]')
IDENT = re.compile(r'[A-Za-z_][A-Za-z_0-9]*\Z')


@dataclass
class Token:
    value: str
    start: int
    end: int


@dataclass
class Symbol:
    name: str
    scope: tuple
    kind: str
    values: list = field(default_factory=list)

    @property
    def path(self):
        return self.scope + (self.name,)


@dataclass
class Reference:
    name: str
    scope: tuple
    start: int
    end: int
    default: object = None


class Schema:
    def __init__(self, source, output_language='java'):
        self.source = source
        self.output_language = output_language
        self.has_objc_prefix = False
        self.tokens = [Token(m[0], m.start(), m.end()) for m in TOKEN.finditer(source.text)
                       if not m[0].isspace() and not m[0].startswith(('//', '/*'))]
        self.index = 0
        self.symbols = {}
        self.references = []
        self.imports = []
        self.package = ''
        self.package_span = None
        self.syntax_end = None
        self.edits = []
        self.parse_scope(())
        if self.syntax_end is None:
            self.fail('HTML 协议必须显式声明 syntax = "proto2" 或 "proto3"')
        # 解析后仅枚举值与默认值还需要原 Token；其余 Token 不参与跨文件解析。
        self.tokens.clear()

    def fail(self, message):
        token = self.tokens[min(self.index, len(self.tokens) - 1)] if self.tokens else None
        line = self.source.text.count('\n', 0, token.start) + 1 if token else 1
        raise ProtoError(f'{self.source.name}:{line} ({", ".join(self.source.titles)}): {message}')

    def peek(self):
        return self.tokens[self.index].value if self.index < len(self.tokens) else ''

    def pop(self, expected=None):
        if self.index >= len(self.tokens):
            self.fail(f'文件意外结束，期望 {expected or "声明"}')
        token = self.tokens[self.index]
        if expected is not None and token.value != expected:
            self.fail(f'期望 {expected!r}，实际 {token.value!r}')
        self.index += 1
        return token

    def identifier(self):
        if not IDENT.fullmatch(self.peek()):
            self.fail('期望标识符')
        return self.pop()

    def qualified(self):
        start = self.tokens[self.index].start if self.index < len(self.tokens) else 0
        name = ''
        if self.peek() == '.':
            name = self.pop().value
        name += self.identifier().value
        while self.peek() == '.':
            name += self.pop().value + self.identifier().value
        return name, start, self.tokens[self.index - 1].end

    def statement(self):
        result = []
        depth = 0
        while self.peek():
            token = self.pop()
            result.append(token)
            if token.value in ('{', '[', '('):
                depth += 1
            elif token.value in ('}', ']', ')'):
                depth -= 1
            if depth < 0:
                self.fail('声明缺少分号或括号不匹配')
            if token.value == ';' and depth == 0:
                return result
        self.fail('声明缺少分号')

    def add_symbol(self, symbol):
        if symbol.path in self.symbols:
            self.fail('同一文件内类型重名: ' + '.'.join(symbol.path))
        self.symbols[symbol.path] = symbol

    def parse_scope(self, scope, closing=False, oneof=False):
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
                start = self.pop().start
                self.package = self.qualified()[0]
                self.package_span = (start, self.pop(';').end)
            elif kind == 'import' and not scope:
                self.pop()
                modifier = self.pop().value if self.peek() in ('public', 'weak') else ''
                value = self.pop().value
                if not value.startswith(('"', "'")) or '\\' in value:
                    self.fail('import 必须是未转义的字符串路径')
                self.imports.append((safe_name(value[1:-1]), modifier))
                self.pop(';')
            elif kind in ('message', 'enum') and not oneof:
                self.pop()
                name = self.identifier().value
                symbol = Symbol(name, scope, kind)
                self.add_symbol(symbol)
                self.pop('{')
                if kind == 'message':
                    self.parse_scope(symbol.path, closing=True)
                else:
                    while self.peek() and self.peek() != '}':
                        if self.peek() in ('option', 'reserved', ';'):
                            self.statement()
                        else:
                            value = self.identifier()
                            self.pop('=')
                            self.statement()
                            symbol.values.append(value)
                    self.pop('}')
            elif kind == 'oneof' and scope and not oneof:
                self.pop(); self.identifier(); self.pop('{')
                self.parse_scope(scope, closing=True, oneof=True)
            elif kind in ('option', 'reserved'):
                statement = self.statement()
                if kind == 'option' and not scope and len(statement) > 1 and statement[1].value == 'objc_class_prefix':
                    self.has_objc_prefix = True
                if self.output_language == 'java' and kind == 'option' and not scope and len(statement) > 1 and statement[1].value.startswith('java_'):
                    self.fail('HTML 规范化暂不接受已有 java_* 选项，请使用普通 proto 编译')
            elif scope:
                if kind in ('extensions', 'extend', 'group'):
                    self.fail(f'HTML 规范化不支持 {kind}，请使用普通 proto 编译')
                if kind in ('required', 'optional', 'repeated'):
                    self.pop()
                if self.peek() == 'group':
                    self.fail('HTML 规范化不支持 group')
                if self.peek() == 'map':
                    self.pop(); self.pop('<'); self.identifier(); self.pop(',')
                    name, start, end = self.qualified()
                    self.pop('>')
                else:
                    name, start, end = self.qualified()
                self.identifier(); self.pop('=')
                tail = self.statement()
                default = None
                for i, token in enumerate(tail[:-2]):
                    if token.value == 'default' and tail[i + 1].value == '=':
                        default = tail[i + 2]
                self.references.append(Reference(name, scope, start, end, default))
            else:
                self.fail(f'HTML 规范化不支持声明 {kind!r}，请使用普通 proto 编译')
        if closing:
            self.pop('}')
        elif self.peek():
            self.fail('多余的右括号')


def namespace_key(name):
    key = re.sub(r'[^a-z0-9_]', '_', str(PurePosixPath(name).with_suffix('')).lower())
    if key[0].isdigit() or key in JAVA_RESERVED:
        key = 'p_' + key
    return key


def validate_java_package(value):
    if not value or any(not IDENT.fullmatch(part) or part in JAVA_RESERVED for part in value.split('.')):
        raise ProtoError(f'无效 Java 包名: {value!r}')
    return value


def normalize(sources, java_package='device.protocol', output_language='java'):
    if output_language not in ('java', 'objc'):
        raise ProtoError('输出语言必须为 Java 或 Objective-C')
    if java_package and output_language == 'java':
        validate_java_package(java_package)
    schemas = {name: Schema(source, output_language=output_language) for name, source in sources.items() if name.endswith('.proto')}
    keys = {name: namespace_key(name) for name in schemas}
    if len(set(keys.values())) != len(keys):
        raise ProtoError('规范化后的文件命名空间冲突，请调整文件名')
    for name, schema in schemas.items():
        for dependency, _ in schema.imports:
            if dependency not in schemas:
                raise ProtoError(f'{name} ({schema.source.titles}): 缺失 import {dependency}')

    visible_cache = {}

    def visible_files(name):
        if name in visible_cache:
            return visible_cache[name]
        visible = {name}
        pending = [dep for dep, _ in schemas[name].imports]
        # 使用显式栈，避免递归闭包循环把整个 schemas 与 Token 保留到下一轮 GC。
        while pending:
            dep = pending.pop()
            if dep in visible:
                continue
            visible.add(dep)
            pending.extend(child for child, modifier in schemas[dep].imports if modifier == 'public')
        visible_cache[name] = tuple(sorted(visible))
        return visible_cache[name]

    def resolve(name, reference):
        # 本地词法作用域优先；导入文件仅在可见集合内参与解析，不做全局猜测。
        schema = schemas[name]
        parts = tuple(reference.name.lstrip('.').split('.'))
        visible = visible_files(name)
        original_prefix = tuple(schema.package.split('.')) if schema.package else ()
        lexical_scope = original_prefix + reference.scope
        prefixes = [()] if reference.name.startswith('.') else [lexical_scope[:i] for i in range(len(lexical_scope), -1, -1)]
        for prefix in prefixes:
            candidate = prefix + parts
            matches = []
            shadowed = []
            for dep in visible:
                dep_schema = schemas[dep]
                dep_prefix = tuple(dep_schema.package.split('.')) if dep_schema.package else ()
                if candidate[:len(dep_prefix)] == dep_prefix:
                    symbol = dep_schema.symbols.get(candidate[len(dep_prefix):])
                    if symbol:
                        matches.append((dep, symbol))
                first = prefix + parts[:1]
                if first[:len(dep_prefix)] == dep_prefix and first[len(dep_prefix):] in dep_schema.symbols:
                    shadowed.append(dep)
            local = [match for match in matches if match[0] == name]
            if local:
                return local[0]
            if name in shadowed:
                raise ProtoError(f'{name}: 本地类型遮蔽了引用 {reference.name}，但该成员不存在')
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ProtoError(f'{name} ({schema.source.titles}): 类型 {reference.name} 引用不唯一: {[m[0] for m in matches]}')
            if shadowed:
                raise ProtoError(f'{name}: 无法在已解析的类型中找到 {reference.name}')
        raise ProtoError(f'{name} ({schema.source.titles}): 无法解析类型 {reference.name}')

    changes = []
    enum_renames = {}
    for name, schema in schemas.items():
        siblings = defaultdict(list)
        for symbol in schema.symbols.values():
            siblings[(symbol.scope, symbol.name)].append(('type', symbol, None))
            if symbol.kind == 'enum':
                for value in symbol.values:
                    siblings[(symbol.scope, value.value)].append(('value', symbol, value))
        occupied = set(siblings)
        for (scope, value_name), entries in siblings.items():
            if len(entries) <= 1:
                continue
            for kind, symbol, token in entries:
                if kind != 'value':
                    continue
                new_name = symbol.name + '_' + value_name
                if (scope, new_name) in occupied:
                    raise ProtoError(f'{name}: 枚举值规范化后仍冲突: {new_name}')
                occupied.add((scope, new_name))
                schema.edits.append((token.start, token.end, new_name))
                enum_renames[(name, symbol.path, value_name)] = new_name
                changes.append({'file': name, 'kind': 'enum_value', 'from': '.'.join(symbol.path + (value_name,)), 'to': new_name})

    result = {}
    for name, schema in schemas.items():
        package = 'device_protocol.' + keys[name]
        # 显式空 Java 包覆盖 proto package，保留协议命名空间来隔离同名类型。
        java = java_package + '.' + keys[name] if java_package else ''
        declaration = f'\npackage {package};\n'
        if output_language == 'java':
            declaration += f'option java_package = "{java}";\n'
        elif not schema.has_objc_prefix:
            # ObjC 类名不会带上 proto 包名，以文件唯一命名空间避免同名类型互相覆盖。
            prefix = 'PCT' + keys[name] + '_'
            declaration += f'option objc_class_prefix = "{prefix}";\n'
            changes.append({'file': name, 'kind': 'objc_prefix', 'from': '', 'to': prefix,
                            'reason': f'设置 Objective-C 类名前缀 {prefix}'})
        if schema.package_span:
            schema.edits.append((*schema.package_span, ''))
        schema.edits.append((schema.syntax_end, schema.syntax_end, declaration))
        changes.append({'file': name, 'kind': 'package', 'from': schema.package, 'to': package, 'java_package': java})
        for reference in schema.references:
            if reference.name in SCALARS:
                continue
            dep, symbol = resolve(name, reference)
            replacement = '.device_protocol.' + keys[dep] + '.' + '.'.join(symbol.path)
            schema.edits.append((reference.start, reference.end, replacement))
            changes.append({'file': name, 'kind': 'type_reference', 'line': schema.source.text.count('\n', 0, reference.start) + 1, 'from': reference.name, 'to': replacement})
            if reference.default is not None:
                token = reference.default
                new_name = enum_renames.get((dep, symbol.path, token.value))
                if new_name:
                    schema.edits.append((token.start, token.end, new_name))
                    changes.append({'file': name, 'kind': 'enum_default', 'from': token.value, 'to': new_name})
        text = schema.source.text
        for start, end, replacement in sorted(schema.edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        result[name] = Source(name, text, schema.source.titles)
    return result, changes
