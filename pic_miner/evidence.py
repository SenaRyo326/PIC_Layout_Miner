"""Static feature ranking with a single, global evidence character budget."""

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import List

from .config import MAX_EVIDENCE_CHARS, MinerError, Sanitizer

OPTICAL_KEYWORDS = ["photonic", "photonics", "pic", "optical", "optics", "waveguide", "siepic",
                    "gdsfactory", "mzi", "ring resonator", "grating coupler", "interferometer",
                    "phase shifter", "heater", "modulator", "photodetector", "硅光", "光子",
                    "光波导", "tfln", "lnoi", "pockels", "lithium niobate", "silicon nitride"]
OPTICAL_PATTERN = re.compile(r'(?<![a-z0-9])(?:' + '|'.join(
    re.escape(word).replace(r'\ ', r'[\s_-]+') + (r's?' if word in {
        'waveguide', 'ring resonator', 'grating coupler', 'interferometer', 'phase shifter',
        'heater', 'modulator', 'photodetector'} else '') for word in OPTICAL_KEYWORDS) + r')(?![a-z0-9])', re.I)


def optical_matches(text):
    """Token boundaries prevent 'picture', 'application' or 'spice' from matching PIC."""
    return sorted({' '.join(re.split(r'[\s_-]+', match.group().lower()))
                   for match in OPTICAL_PATTERN.finditer(str(text))})


def is_markdown(path):
    return PurePosixPath(path.lower()).suffix in {'.md', '.markdown', '.mdown'}


def markdown_slices(documents, limit=3000):
    """Rank paragraphs across files; retain source paths and late physical anchors."""
    records = []
    for index, (path, raw) in enumerate(documents):
        for position, paragraph in enumerate(re.split(r'\n\s*\n|\n(?=#{1,6}\s)', raw)):
            if not paragraph.strip():
                continue
            score = sum(weight for _, pattern, weight in FEATURES if pattern.search(paragraph))
            score += 80 if OPTICAL_PATTERN.search(paragraph) else 0
            score += 90 if re.search(r'\bpdk\b|\bports?\b|\bmetal\b|版图|端口', paragraph, re.I) else 0
            compact = paragraph.strip() if len(paragraph) <= 650 else snippets(paragraph, 650).strip()
            records.append((score, index, position, 'File: ' + path + '\n' + compact))
    ordered = sorted(records, key=lambda value: (-value[0], value[1], value[2]))
    selected, used, represented = [], 0, set()
    def retain(record):
        nonlocal used
        block = record[3] + '\n\n'
        if used + len(block) <= limit:
            selected.append(record)
            used += len(block)
            represented.add(record[1])
    for record in ordered:
        if record[1] not in represented:
            retain(record)
    retained = {(record[1], record[2]) for record in selected}
    for record in ordered:
        if (record[1], record[2]) not in retained:
            retain(record)
    return ''.join(record[3] + '\n\n' for record in selected)[:limit]


LAYOUT_SUFFIXES = {".gds", ".gdsii", ".oas", ".oasis"}
SCRIPT_SUFFIXES = {".py", ".ipynb", ".jl", ".rb", ".js", ".ts", ".lua", ".lym", ".yaml", ".yml", ".m", ".tcl", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rs", ".sh"}
EXPORT_PATTERN = re.compile(
    r"\b(?:write_gds|write_oas(?:is)?|export_gds|export_oas(?:is)?|save_gds|to_gds)\b"
    r"|\.\s*(?:write|save|export)\s*\("
    r"|\b(?:write|save|export)\s*\([^\n]{0,160}\.(?:gds|gdsii|oas|oasis)\b"
    r"|\b(?:gdsii|gds)_(?:write|export)\b", re.IGNORECASE)
LAYOUT_EXPORT_PATTERN = re.compile(
    r"\b(?:write_gds|write_oas(?:is)?|export_gds|export_oas(?:is)?|save_gds|to_gds)\b"
    r"|\b(?:write|save|export)\s*\([^\n]{0,160}\.(?:gds|gdsii|oas|oasis)\b"
    r"|\b(?:gdsii|gds)_(?:write|export)\b", re.IGNORECASE)
ELECTRICAL_PATTERN = re.compile(
    r"\b(?:heater\w*|phase[_ -]?shift\w*|pad(?:s|_\w+)?|via(?:s|_\w+)?|electrod\w*|"
    r"modulat\w*|photodetect\w*|electrical\w*)\b|电极|热调谐|电端口", re.IGNORECASE)
PORT_PATTERN = re.compile(
    r"\b(?:add_ports?|electrical_ports?|port_type|bond_pad)\b|电端口|端口连接", re.IGNORECASE)
PORT_REFERENCE_PATTERN = re.compile(r"\.\s*ports\b|\bports\s*\[", re.IGNORECASE)
CONNECTION_PATTERN = re.compile(
    r"\b(?:connect|route_\w+|add_ref|add_array)\s*\(|<<|顶层|互连", re.IGNORECASE)
MANUFACTURING_PATTERN = re.compile(
    r"\btape[_ -]?out\b|\bmpw\b|\bfabricat\w*\b|\bmanufactur\w*\b|"
    r"thermal[_ -]?tuning|thermo[_ -]?optic|流片|制造|热调谐", re.IGNORECASE)
PIC_PATTERN = re.compile(r"photon\w*|\bpic\b|siepic|gdsfactory|硅光|光子", re.IGNORECASE)
MULTICHANNEL_PATTERN = re.compile(
    r"(?:multi[_ -]?channel|\d+[_ -]?channel)[^\n]{0,100}(?:electrod|heater|modulat|phase)|"
    r"多通道[^\n]{0,30}(?:电极|加热|调制)|电极阵列", re.IGNORECASE)
SIGNAL_PATTERN = re.compile(
    r"photon|siepic|gdsfactory|heater|electrod|modulat|photodetect|phase.?shift|"
    r"tape.?out|fabricat|mpw|reticle|mesh|array|pad\b|via\b|electrical|write_gds|write_oas",
    re.IGNORECASE)
PHYSICAL_RESOURCE_PATTERN = re.compile(r'Git LFS|original_size|\.(?:gds|gdsii|oas|oasis)(?:\.gz)?\b|release_asset|external hosted', re.I)
FEATURES = (("physical_resource", PHYSICAL_RESOURCE_PATTERN, 170), ("layout_export", LAYOUT_EXPORT_PATTERN, 160), ("ports", PORT_PATTERN, 155),
            ("port_references", PORT_REFERENCE_PATTERN, 145), ("write", EXPORT_PATTERN, 140),
            ("optical", OPTICAL_PATTERN, 120), ("electrical", ELECTRICAL_PATTERN, 110), ("connections", CONNECTION_PATTERN, 105),
            ("manufacturing", MANUFACTURING_PATTERN, 100))


@dataclass
class FileEntry:
    path: str
    sha: str = ""
    size: int = 0
    download_url: str = ""


@dataclass
class Candidate:
    source: str
    resource_id: str
    version: str
    name: str
    url: str
    description: str = ""
    metadata: dict = field(default_factory=dict)
    files: List[FileEntry] = field(default_factory=list)


def is_layout(path: str) -> bool:
    lower = path.lower()
    return PurePosixPath(lower).suffix in LAYOUT_SUFFIXES or any(
        lower.endswith(suffix + ".gz") for suffix in LAYOUT_SUFFIXES)


def is_script(path: str) -> bool:
    return PurePosixPath(path.lower()).suffix in SCRIPT_SUFFIXES


def is_readme(path: str) -> bool:
    return PurePosixPath(path.lower()).name in {"readme", "readme.md", "readme.rst", "readme.txt"}


def priority(entry: FileEntry) -> tuple:
    path = entry.path.lower()
    relevant = bool(SIGNAL_PATTERN.search(path) or re.search(r"layout|mask|chip|design|export", path))
    return not relevant, path.count("/"), path


def strong_description(text: str) -> bool:
    """Positive nomination: photonics plus manufacture, or specific electric cues."""
    tool = bool(re.search(r'gdsfactory|siepic|phidl|nazca|gdstk|gdspy', text, re.I) and
                re.search(r'layout|gds|oas|mask|版图', text, re.I)) or bool(
                    PIC_PATTERN.search(text) and re.search(r'layout|版图', text, re.I) and
                    re.search(r'library|toolkit|framework|pdk|generator|库|工具', text, re.I))
    return bool(tool or (PIC_PATTERN.search(text) and MANUFACTURING_PATTERN.search(text))
                or MULTICHANNEL_PATTERN.search(text)
                or re.search(r"thermal[_ -]?tuning|thermo[_ -]?optic|热调谐", text, re.IGNORECASE))


def plain_text(path: str, raw: str) -> str:
    if not path.lower().endswith(".ipynb"):
        return raw
    try:
        notebook = json.loads(raw)
        if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
            raise ValueError("notebook shape")
        parts = []
        for index, cell in enumerate(notebook["cells"]):
            if not isinstance(cell, dict):
                raise ValueError("cell shape")
            if cell.get("cell_type", "code") != "code":
                continue
            source = cell.get("source", [])
            if isinstance(source, str):
                code = source
            elif isinstance(source, list) and all(isinstance(line, str) for line in source):
                code = "".join(source)
            else:
                raise ValueError("source shape")
            parts.append("Cell " + str(index + 1) + ":\n" + code)
        return "\n".join(parts)
    except (ValueError, TypeError, RecursionError):
        # Never fall back to raw JSON: outputs may contain megabytes of images.
        raise MinerError("Notebook 结构无法解析；未将 output/image 数据作为代码证据。") from None


def _compact_line(line: str) -> str:
    if len(line) <= 600:
        return line
    intervals = []
    for _, pattern, _ in FEATURES:
        match = pattern.search(line)
        if match:
            intervals.append((max(0, match.start() - 45), min(len(line), match.end() + 65)))
    if not intervals:
        return line[:300] + " …"
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return " … ".join(line[start:end] for start, end in merged)


def _ranked_sections(sections, limit: int, headers: bool):
    """Reserve each feature class first, then anchors, context, and ordinary lines."""
    records = []
    compacted = False
    for section, (_, text) in enumerate(sections):
        lines = text.splitlines()
        weights = [25 if SIGNAL_PATTERN.search(line) else 1 for line in lines]
        classes = []
        for index, line in enumerate(lines):
            features = {name for name, pattern, _ in FEATURES if pattern.search(line)}
            classes.append(features)
            for name, _, weight in FEATURES:
                if name in features:
                    weights[index] = max(weights[index], weight)
                    for neighbor in range(max(0, index - 2), min(len(lines), index + 4)):
                        weights[neighbor] = max(weights[neighbor], weight // (3 + abs(neighbor - index)))
        for index, line in enumerate(lines):
            compact = _compact_line(line)
            compacted = compacted or compact != line
            records.append((section, index, weights[index], classes[index], compact))
    selected = {}
    used = 0
    represented = set()

    def retain(record):
        nonlocal used
        section, index, _, _, line = record
        key = (section, index)
        if key in selected:
            return True
        prefix = ("[E" + str(section + 1) + "] " + sections[section][0][:200] + "\n") if headers else ""
        overhead = len(prefix) if section not in represented else 0
        rendered = "L" + str(index + 1) + ": " + line + "\n"
        if used + overhead + len(rendered) > limit:
            return False
        selected[key] = rendered
        represented.add(section)
        used += overhead + len(rendered)
        return True

    ordered = sorted(records, key=lambda item: (-item[2], item[0], item[1]))
    # Each category gets a representative even when many exports compete.
    for name, _, _ in FEATURES:
        for record in ordered:
            if name in record[3] and retain(record):
                break
    # Reserve neighboring lines for those representative anchors before filling
    # the remaining space with potentially thousands of duplicate anchors.
    representative_keys = set(selected)
    by_key = {(record[0], record[1]): record for record in records}
    for section, index in sorted(representative_keys):
        for offset in (-1, 1, -2, 2):
            neighbor = by_key.get((section, index + offset))
            if neighbor:
                retain(neighbor)
    # Reserve identity and a core anchor in each source file before filling gaps.
    for section in range(len(sections)):
        candidates = [record for record in ordered if record[0] == section]
        if candidates:
            retain(candidates[0])
        first = next((record for record in records if record[0] == section and record[1] == 0), None)
        if first:
            retain(first)
    for record in ordered:
        retain(record)
    parts = []
    for section, (label, _) in enumerate(sections):
        lines = [value for (sid, _), value in sorted(selected.items()) if sid == section]
        if lines:
            if headers:
                parts.append("[E" + str(section + 1) + "] " + label[:200] + "\n")
            parts.extend(lines)
    dropped = sum(bool(record[3]) and (record[0], record[1]) not in selected for record in records)
    return "".join(parts), compacted or len(selected) < len(records), dropped


def snippets(text: str, limit: int = 2200) -> str:
    return _ranked_sections([("", text)], max(0, limit), headers=False)[0]


class EvidenceBuilder:
    def __init__(self, candidate: Candidate, sanitizer: Sanitizer):
        self.candidate = candidate
        self.sanitizer = sanitizer
        self.sections = []
        self.raw_sections = []
        self.rules = []
        self.notes = []
        self.script_count = 0
        self.matched = False
        self.inspection_incomplete = False
        description = html.unescape(re.sub(r"<[^>]+>", " ", candidate.description))
        metadata = json.dumps(candidate.metadata, ensure_ascii=False, sort_keys=True)
        self.description_context = candidate.name + "\n" + description + "\n" + metadata
        self.optical_keywords = set(optical_matches(self.description_context))
        self.descriptive_match = strong_description(self.description_context)
        self.add("resource metadata", self.description_context)

    def add(self, label: str, content: str) -> None:
        label = self.sanitizer.clean(label).replace("\r", " ").replace("\n", " ")
        self.sections.append((label, self.sanitizer.clean(content)))
        self.raw_sections.append((label, self.sanitizer.clean(content)))

    def preserve(self, label: str, content: str) -> None:
        """Retain bounded, cleaned source independently of the compact preview."""
        self.raw_sections.append((self.sanitizer.clean(label), self.sanitizer.clean(content)))

    def inspect_tree(self, files: List[FileEntry]) -> bool:
        for entry in files:
            self.optical_keywords.update(optical_matches(entry.path))
        layouts = sorted((entry for entry in files if is_layout(entry.path)), key=priority)
        self.matched = bool(layouts)
        if layouts:
            self.rules.append("layout_file")
        ordered = layouts + sorted((entry for entry in files if not is_layout(entry.path)), key=priority)
        self.add("file inventory (not binary contents)", "\n".join(
            entry.path + " (" + str(entry.size) + " bytes)" for entry in ordered[:120]))
        if len(ordered) > 120:
            self.preserve('complete file inventory (not binary contents)', '\n'.join(
                entry.path + ' (' + str(entry.size) + ' bytes)' for entry in ordered))
        if len(ordered) > 120:
            self.notes.append("文件清单仅展示优先排序的前 120 项；规则检查覆盖全部已枚举路径。")
        return bool(layouts)

    def inspect_script(self, entry: FileEntry, raw: str) -> bool:
        return self.inspect_text(entry, plain_text(entry.path, raw))

    def inspect_text(self, entry: FileEntry, text: str) -> bool:
        """Inspect already-cleaned source so large notebooks are parsed only once."""
        self.script_count += 1
        self.optical_keywords.update(optical_matches(text))
        export = bool(EXPORT_PATTERN.search(text))
        if export:
            self.matched = True
            if "export_script" not in self.rules:
                self.rules.append("export_script")
        return export

    def add_file(self, entry: FileEntry, raw: str) -> None:
        self.add_text_file(entry, plain_text(entry.path, raw))

    def add_text_file(self, entry: FileEntry, text: str) -> None:
        self.optical_keywords.update(optical_matches(text))
        if is_readme(entry.path) and strong_description(self.description_context + "\n" + text):
            self.descriptive_match = True
        self.add(entry.path, text)

    def mark_incomplete(self, note: str) -> None:
        self.inspection_incomplete = True
        self.notes.append(self.sanitizer.clean(note))

    def build(self, limit=MAX_EVIDENCE_CHARS) -> dict:
        rules = list(self.rules)
        if self.descriptive_match:
            rules.append("strong_description")
        notes = self.notes + ["仅静态分析；未执行脚本，未验证 GDS/OASIS 几何内容。",
                              "静态检查脚本数：" + str(self.script_count)]
        # Rank the entire package, not each file followed by a head slice.
        limit = min(MAX_EVIDENCE_CHARS, max(500, limit))
        reserve = min(1100, max(300, limit // 5))
        blocks = []
        seen = set()
        import hashlib
        for label, content in self.raw_sections:
            digest = hashlib.sha256(content.encode()).hexdigest()
            if (label, digest) not in seen:
                blocks.append({'id': 'E' + str(len(blocks) + 1), 'path': label,
                               'text': content, 'sha256': digest,
                               'source': self.candidate.source, 'version': self.candidate.version})
                seen.add((label, digest))
        # Extra preserved sources must not create a parallel E-number namespace.
        block_ids = {(block['path'], block['sha256']): block['id'] for block in blocks}
        preview_ids = [block_ids[(label, hashlib.sha256(content.encode()).hexdigest())]
                       for label, content in self.sections]
        extra_id_chars = sum(max(0, len(identifier) - len('E' + str(index)))
                             for index, identifier in enumerate(preview_ids, start=1))
        text, truncated, dropped = _ranked_sections(self.sections, max(0, limit - reserve - extra_id_chars), headers=True)
        text = re.sub(r'(?m)^\[E(\d+)\] ',
                      lambda match: '[' + preview_ids[int(match.group(1)) - 1] + '] ', text)
        if dropped:
            notes.append("字符预算内已优先保留各特征类别；另有 " + str(dropped) + " 条重复/额外锚点未展示。")
        header = "[E" + str(len(blocks) + 1) + "] coverage and limitations\n"
        tail = header + "\n".join(notes)[:reserve - len(header)]
        text += tail
        assert len(text) <= limit

        blocks.append({'id': 'E' + str(len(blocks) + 1), 'path': 'coverage and limitations',
                       'text': '\n'.join(notes), 'source': self.candidate.source,
                       'version': self.candidate.version})
        return {"text": text, "blocks": blocks, "coverage_notes": notes,
                "matched": self.matched, "strong_description": self.descriptive_match,
                "inspection_incomplete": self.inspection_incomplete, "rules": rules,
                "truncated": truncated, "dropped_anchor_lines": dropped,
                "optical_match": bool(self.optical_keywords), "optical_keywords": sorted(self.optical_keywords),
                "paths": [self.sanitizer.clean(entry.path) for entry in self.candidate.files if is_layout(entry.path)][:50]}
