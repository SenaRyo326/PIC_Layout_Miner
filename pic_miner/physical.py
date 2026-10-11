"""Static physical-resource clues: releases, LFS specifications and hosted URLs."""

import fnmatch
import re
import shlex
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .evidence import is_layout

LAYOUT_NAME = re.compile(r'gds|oasis|layout|mask|tape.?out|reticle|pic[_-]|\bpic\b|photonic|版图|流片', re.I)
ARCHIVES = ('.zip', '.tar.gz', '.tgz')


def layout_asset(name):
    return is_layout(name) or (name.lower().endswith(ARCHIVES) and bool(LAYOUT_NAME.search(name)))


def lfs_pointer(raw):
    if isinstance(raw, bytes):
        try:
            raw = raw.decode('ascii')
        except UnicodeError:
            return None
    if not isinstance(raw, str) or len(raw) > 4096:
        return None
    lines = raw.strip().splitlines()
    if not lines or lines[0].strip() != 'version https://git-lfs.github.com/spec/v1':
        return None
    oids = re.findall(r'(?m)^oid sha256:([a-fA-F0-9]{64})[ \t]*$', raw)
    sizes = re.findall(r'(?m)^size ([0-9]+)[ \t]*$', raw)
    if len(oids) != 1 or len(sizes) != 1:
        return None
    amount = int(sizes[0])
    return {'oid': 'sha256:' + oids[0].lower(), 'original_size': amount} if 0 < amount < 2 ** 63 else None


def attribute_rules(path, text):
    rules = []
    directory = str(PurePosixPath(path).parent)
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if len(tokens) < 2 or tokens[0].startswith('!'):
            continue
        tracked = None
        for token in tokens[1:]:
            if token == 'filter=lfs':
                tracked = True
            elif token in {'-filter', '!filter'} or token.startswith('filter='):
                tracked = False
        if tracked is not None:
            rules.append((directory, tokens[0], tracked))
    return rules


def tracked_lfs(path, rules):
    tracked = False
    for directory, pattern, value in rules:
        prefix = '' if directory == '.' else directory + '/'
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        target = relative if '/' in pattern else PurePosixPath(relative).name
        if fnmatch.fnmatchcase(target, pattern):
            tracked = value
    return tracked


def binary_layout(raw):
    if raw.startswith(b'\x1f\x8b'):
        import gzip
        import io
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(14)
        except (OSError, EOFError, ValueError):
            return False
    return raw.startswith(b'\x00\x06\x00\x02') or raw.startswith(b'%SEMI-OASIS\r\n')


def hosted_links(text):
    from .links import URL_PATTERN
    result = {}
    for match in URL_PATTERN.finditer(text):
        url = match[0].rstrip('.,;:)]}')
        try:
            parts = urlsplit(url)
            host = (parts.hostname or '').lower()
            if parts.username or parts.password or parts.port not in (None, 80, 443):
                continue
        except ValueError:
            continue
        kinds = {'zenodo.org': 'Zenodo', 'doi.org': 'DOI', 'osf.io': 'OSF',
                 'drive.google.com': 'Google Drive', 'docs.google.com': 'Google Drive',
                 'dropbox.com': 'Dropbox', 'www.dropbox.com': 'Dropbox', '1drv.ms': 'OneDrive',
                 'onedrive.live.com': 'OneDrive', 'pan.baidu.com': '百度网盘', 'www.jianguoyun.com': '坚果云'}
        kind = kinds.get(host)
        if kind:
            context = text[max(0, match.start() - 250):match.end() + 250]
            result[url] = {'kind': kind, 'url': url, 'path': kind + ' 外部托管入口',
                           'layout_claim': bool(LAYOUT_NAME.search(context))}
    for match in re.finditer(r'(?<![\w.])10\.5281/zenodo\.\d+', text):
        url = 'https://doi.org/' + match[0]
        context = text[max(0, match.start() - 250):match.end() + 250]
        result.setdefault(url, {'kind': 'Zenodo DOI', 'url': url, 'path': match[0],
                                'layout_claim': bool(LAYOUT_NAME.search(context))})
    return list(result.values())
