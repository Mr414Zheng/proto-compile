import hashlib
import json
import sys
from pathlib import Path

from . import ProtoError
from .extract import Source, safe_name
from .normalize import Schema

RESOURCE_ROOT = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
DEFAULT_REPAIRS = RESOURCE_ROOT / 'config' / 'device-v16.2-repairs.json'


def repair_sources(sources, config=DEFAULT_REPAIRS, strict=False, warnings=None):
    try:
        rules = json.loads(Path(config).read_text(encoding='utf-8'))
        if not isinstance(rules, dict):
            raise ValueError('根节点必须是对象')
    except (ValueError, OSError) as exc:
        raise ProtoError(f'无法读取修正规则 {config}: {exc}') from exc
    result = dict(sources)
    changes = []
    for name, rule in rules.items():
        if not isinstance(rule, dict) or not isinstance(rule.get('sha256'), str):
            raise ProtoError(f'修正规则 {name} 必须声明 sha256')
        if name not in sources:
            if strict:
                raise ProtoError(f'修正规则文件不存在: {name}')
            continue
        source = sources[name]
        if hashlib.sha256(source.text.encode('utf-8')).hexdigest() != rule['sha256']:
            if strict:
                raise ProtoError(f'修正规则源文件校验不匹配: {name}')
            if warnings is not None:
                warnings.append(f'修正规则源文件校验不匹配，已跳过修正: {name}')
            continue
        schema = Schema(source)
        edits = []
        imports = rule.get('add_imports', [])
        replacements = rule.get('type_replacements', {})
        if not isinstance(imports, list) or not all(isinstance(i, str) for i in imports) or not isinstance(replacements, dict):
            raise ProtoError(f'修正规则结构无效: {name}')
        for dependency in imports:
            safe_name(dependency)
            if dependency not in sources:
                raise ProtoError(f'{name}: 修正所需文件缺失 {dependency}')
            if dependency not in {dep for dep, _ in schema.imports}:
                edits.append((schema.syntax_end, schema.syntax_end, '\nimport ' + json.dumps(dependency) + ';\n'))
                changes.append({'file': name, 'kind': 'repair_import', 'to': dependency, 'reason': rule.get('reason', '')})
        for before, after in replacements.items():
            if not isinstance(after, str):
                raise ProtoError(f'{name}: type_replacements 的值必须为字符串')
            refs = [ref for ref in schema.references if ref.name == before]
            if not refs:
                raise ProtoError(f'{name}: 修正找不到类型引用 {before}')
            for ref in refs:
                edits.append((ref.start, ref.end, after))
            changes.append({'file': name, 'kind': 'repair_type', 'from': before, 'to': after, 'reason': rule.get('reason', '')})
        text = source.text
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        result[name] = Source(name, text, source.titles)
    return result, changes
