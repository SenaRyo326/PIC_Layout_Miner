"""Official Atom discovery, lightweight paper reader, optional static full-text inspection."""

import hashlib
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

from .config import MAX_SOURCE_BYTES, MinerError
from .evidence import Candidate, EvidenceBuilder, FileEntry, PIC_PATTERN, optical_matches, snippets
from .links import external_targets
from .judge import PAPER_EVIDENCE_LIMIT

ATOM = '{http://www.w3.org/2005/Atom}'
ARXIV = '{http://arxiv.org/schemas/atom}'
OPEN = '{http://a9.com/-/spec/opensearch/1.1/}'
ID_PATTERN = re.compile(r'(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?')
CLAIM_PATTERN = re.compile(
    r'(?:open[ -]?source|publicly|available|released|repository|github|zenodo)[^.]{0,250}'
    r'(?:layout|gds|oasis|design files|mask|tapeout)|'
    r'(?:layout|gds|oasis|design files|mask)[^.]{0,250}'
    r'(?:open[ -]?source|publicly|available|released|repository|github|zenodo)', re.I)
AVAILABILITY = re.compile(r'(?:data|code|software|design|layout)\s+(?:and\s+\w+\s+)?availability|availability\s+of\s+(?:data|code)|open[ -]?source', re.I)


class PaperHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag == 'a' and not self.hidden:
            href = dict(attrs).get('href')
            if href:
                self.links.append(href)
        if tag in {'p', 'div', 'section', 'h1', 'h2', 'h3', 'br'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def availability_text(text):
    # Prioritize statements at the end of papers, not only their opening pages.
    ranges = []
    for match in AVAILABILITY.finditer(text):
        ranges.append((max(0, match.start() - 200), min(len(text), match.end() + 2500)))
    for match in CLAIM_PATTERN.finditer(text):
        ranges.append((max(0, match.start() - 200), min(len(text), match.end() + 500)))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return '\n\n'.join(text[start:end] for start, end in merged)


class Arxiv:
    def __init__(self, api, content, sanitizer, announce=print):
        self.client = api
        self.content = content
        self.sanitizer = sanitizer
        self.announce = announce

    def search(self, query, start, size=50, sort_by='lastUpdatedDate'):
        xml = self.client.get_text('/query', max_bytes=4 * 1024 * 1024, params={
            'search_query': query, 'start': start, 'max_results': size,
            'sortBy': sort_by, 'sortOrder': 'descending'})
        # Refuse DTD/entity declarations before stdlib parsing.
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', xml, re.I):
            raise MinerError('arXiv Atom 含不允许的 XML 声明。')
        try:
            root = ET.fromstring(xml)
            if root.tag != ATOM + 'feed' or root.find(OPEN + 'totalResults') is None:
                raise ValueError('not an Atom search feed')
            total = int(root.findtext(OPEN + 'totalResults'))
            if total < 0:
                raise ValueError('negative result count')
            entries = []
            for element in root.findall(ATOM + 'entry'):
                identifier = element.findtext(ATOM + 'id', '')
                parts = urlsplit(identifier)
                name = parts.path.removeprefix('/abs/')
                if parts.hostname not in {'arxiv.org', 'export.arxiv.org', 'www.arxiv.org'} or not ID_PATTERN.fullmatch(name):
                    raise ValueError('entry id')
                entries.append({'id': name, 'updated': element.findtext(ATOM + 'updated', ''),
                                'published': element.findtext(ATOM + 'published', ''),
                                'title': ' '.join(element.findtext(ATOM + 'title', '').split()),
                                'summary': element.findtext(ATOM + 'summary', ''),
                                'comment': element.findtext(ARXIV + 'comment', ''),
                                'links': [link.get('href', '') for link in element.findall(ATOM + 'link')]})
            return entries, total
        except (ET.ParseError, ValueError, TypeError):
            raise MinerError('arXiv Atom 解析失败；游标未前移。') from None

    def candidate(self, record):
        version_id = record['id']
        if not isinstance(version_id, str) or not ID_PATTERN.fullmatch(version_id):
            raise MinerError('arXiv 论文标识无效。')
        identifier = re.sub(r'v\d+$', '', version_id)
        version = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        return Candidate('arxiv', identifier, version, self.sanitizer.clean(record.get('title') or identifier),
                         'https://arxiv.org/abs/' + quote(version_id, safe='/'),
                         self.sanitizer.clean(record.get('summary', '')),
                         {'arxiv_id': version_id, 'updated': record.get('updated'),
                          'comment': self.sanitizer.clean(record.get('comment') or record.get('comments') or ''),
                          'links': [self.sanitizer.clean(link) for link in record.get('links', []) if isinstance(link, str)]})

    def _pdf_text(self, data):
        # PDF parser warnings can contain raw remote content. Keep them out of terminal logs.
        logger = logging.getLogger('pypdf')
        handlers, propagate = logger.handlers[:], logger.propagate
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        try:
            return self._parse_pdf(data)
        finally:
            logger.handlers, logger.propagate = handlers, propagate

    def _parse_pdf(self, data):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted or len(reader.pages) > 120:
            raise MinerError('arXiv PDF 加密或超过 120 页静态解析上限。')
        text = []
        links = []
        for page in reader.pages:
            # Reject huge expanded content streams before text extraction.
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > MAX_SOURCE_BYTES:
                raise MinerError('arXiv PDF 页面解压内容超过安全大小限制。')
            piece = page.extract_text() or ''
            text.append(piece)
            if sum(map(len, text)) > 2_000_000:
                raise MinerError('arXiv PDF 文字超过安全提取上限。')
            for annotation in page.get('/Annots', []):
                obj = annotation.get_object()
                action = obj.get('/A')
                if action:
                    action = action.get_object()
                if action and action.get('/URI'):
                    links.append(str(action['/URI']))
        return '\n'.join(text), links

    def evidence(self, candidate):
        """Title/abstract/comments/links only: no HTML/PDF request in the worker."""
        builder = EvidenceBuilder(candidate, self.sanitizer)
        comments = candidate.metadata.get('comment', '')
        hrefs = candidate.metadata.get('links', [])
        combined = '\n'.join([candidate.name, candidate.description, comments, *hrefs])
        targets = external_targets(combined)
        claims = [match.group().strip() for match in CLAIM_PATTERN.finditer(combined)][:6]
        code_claim = bool(re.search(
            r'(?:open[ -]?source|publicly|available|released)[^.]{0,160}(?:\bpdk\b|\bcode\b|repository)|'
            r'(?:\bpdk\b|\bcode\b|repository)[^.]{0,160}(?:open[ -]?source|publicly|available|released)', combined, re.I))
        builder.sections = []
        builder.raw_sections = []
        builder.add('paper metadata: title, abstract, comments',
                    'Title: ' + candidate.name + '\nAbstract: ' + candidate.description + '\nComments: ' + comments)
        if claims:
            builder.add('open PIC design statements', '\n'.join(claims))
        from .physical import hosted_links
        hosts = hosted_links(combined)
        if targets or hosts:
            public_links = [name if source == 'github' else 'Zenodo record ' + name for source, name in targets]
            public_links += [host['url'] for host in hosts]
            builder.add('external code/data links', '\n'.join(public_links))
        builder.descriptive_match = False
        builder.matched = bool(builder.optical_keywords and (claims or code_claim or targets or
                                                              any(host['layout_claim'] for host in hosts)))
        if builder.matched:
            builder.rules.append('paper_open_layout_claim' if claims else 'paper_open_design_resource')
        builder.notes.append('轻量 Paper Reader：仅标题、摘要、评论与外链；未下载论文全文。公开声明需核实。')
        result = builder.build(limit=PAPER_EVIDENCE_LIMIT)
        result.update({'reader_mode': 'paper_metadata', 'physical_layout_confirmed': False,
                       'external_hosts': hosts, 'external_targets': targets,
                       'pointers': [{'kind': source + '_link', 'path': name,
                                     'url': 'https://github.com/' + name if source == 'github' else 'https://zenodo.org/records/' + name}
                                    for source, name in targets] + hosts})
        return result

    def full_evidence(self, candidate):
        """Normal paper retrieval: HTML first, bounded static PDF fallback."""
        builder = EvidenceBuilder(candidate, self.sanitizer)
        abstract = candidate.description + '\n' + candidate.metadata.get('comment', '')
        body = ''
        hrefs = []
        version_id = candidate.metadata['arxiv_id']
        try:
            raw = self.content.get_text('/html/' + quote(version_id, safe='/'), max_bytes=MAX_SOURCE_BYTES)
            parser = PaperHTML()
            parser.feed(raw)
            body = ''.join(parser.parts)
            hrefs = parser.links
            if len(body.strip()) < 100:
                raise MinerError('arXiv HTML 正文不可用。')
        except (MinerError, ValueError, TypeError):
            try:
                data = self.content.get_bytes('/pdf/' + quote(version_id, safe='/'), max_bytes=MAX_SOURCE_BYTES)
                body, hrefs = self._pdf_text(data)
            except Exception as error:
                # External PDF parsers expose many heterogeneous exceptions. Never execute PDF scripts.
                builder.mark_incomplete('arXiv HTML/PDF 正文未能完整静态提取（' + type(error).__name__ + '）。')
        statements = availability_text(body)
        hrefs = list(dict.fromkeys(candidate.metadata.get('links', []) + hrefs))
        combined = candidate.name + '\n' + abstract + '\n' + body
        claims = [' '.join(match.group().split()) for match in CLAIM_PATTERN.finditer(statements or combined)][:6]
        if claims:
            # Separate concise claim section guarantees a representative survives global ranking.
            builder.add_text_file(FileEntry('open_layout_claims.txt'), '\n'.join(claims))
        if statements:
            builder.add_text_file(FileEntry('data_code_availability.txt'), statements)
        if body:
            builder.preserve('paper_fulltext.txt', body)
            builder.add_text_file(FileEntry('paper_context.txt'), snippets(body, 8000))
        if PIC_PATTERN.search(combined) and CLAIM_PATTERN.search(combined):
            builder.matched = True
            builder.rules.append('paper_open_layout_claim')
        builder.notes.append('论文声明只作为候选证据，不证明版图或电极附件存在；未执行外部代码。')
        result = builder.build()
        from .physical import hosted_links
        result['physical_layout_confirmed'] = False
        result['external_hosts'] = hosted_links(combined + '\n' + '\n'.join(hrefs))
        result['pointers'] = result['external_hosts']
        result['external_targets'] = external_targets(combined + '\n' + '\n'.join(hrefs))
        result['reader_mode'] = 'paper_fulltext'
        return result
