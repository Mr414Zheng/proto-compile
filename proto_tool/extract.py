import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath

from . import ProtoError


@dataclass
class Source:
    name: str
    text: str
    titles: list = field(default_factory=list)


class WikiParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.depth = 0
        self.active = None
        self.pre = False
        self.parts = []
        self.json_parts = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div':
            self.depth += 1
            if self.active is None and 'title' in attrs:
                self.active = (self.depth, attrs['title'])
                self.parts = []
        if tag == 'pre' and self.active:
            self.pre = True
        if tag == 'script' and attrs.get('type') == 'application/json' and 'tiddlywiki-tiddler-store' in attrs.get('class', '').split():
            self.json_parts = []

    def handle_data(self, data):
        if self.active and self.pre:
            self.parts.append(data)
        if self.json_parts is not None:
            self.json_parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.pre = False
        if tag == 'div':
            if self.active and self.depth == self.active[0]:
                self.items.append((self.active[1], ''.join(self.parts)))
                self.active = None
            self.depth -= 1
        if tag == 'script' and self.json_parts is not None:
            try:
                for item in json.loads(''.join(self.json_parts)):
                    self.items.append((item['title'], item.get('text', '')))
            except (ValueError, TypeError, KeyError) as exc:
                raise ProtoError(f'TiddlyWiki JSON 数据无效: {exc}') from exc
            finally:
                self.json_parts = None


def safe_name(name):
    parts = name.split('/')
    if (not name or '\\' in name or ':' in name or '\x00' in name
            or PurePosixPath(name).is_absolute()
            or any(p in ('', '.', '..') for p in parts)):
        raise ProtoError(f'不安全的文件名: {name!r}')
    return name


def extract(path):
    parser = WikiParser()
    parser.feed(path.read_text(encoding='utf-8-sig'))
    sources = {}
    marker = re.compile(r'(?m)^[ \t]*//[ \t]*\$\(FILE\)([^\r\n]+)[ \t]*$')
    for title, body in parser.items:
        if title.startswith('$:/'):
            continue
        matches = list(marker.finditer(body))
        for index, match in enumerate(matches):
            name = match[1].strip()
            if not name.endswith(('.proto', '.options')):
                continue
            safe_name(name)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            content = body[match.end():end].strip() + '\n'
            if name in sources:
                if sources[name].text != content:
                    raise ProtoError(f'同名文件内容冲突: {name} ({sources[name].titles} / {title})')
                sources[name].titles.append(title)
            else:
                sources[name] = Source(name, content, [title])
    if not any(n.endswith('.proto') for n in sources):
        raise ProtoError('文档中未找到带 $(FILE) 标记的 proto 声明')
    folded = {}
    for name in sources:
        key = name.casefold()
        if key in folded and folded[key] != name:
            raise ProtoError(f'文件名大小写冲突: {folded[key]} / {name}')
        folded[key] = name
    return sources


def write_sources(sources, directory):
    directory.mkdir(parents=True, exist_ok=True)
    for source in sources.values():
        target = directory / safe_name(source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.text, encoding='utf-8')
