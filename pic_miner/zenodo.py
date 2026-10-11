"""Optional, public Zenodo record discovery; metadata/files only."""

import hashlib
import json
from urllib.parse import quote

from .config import MAX_SOURCE_BYTES, MAX_TEXT_BYTES, MinerError
from .evidence import Candidate, EvidenceBuilder, FileEntry, is_readme, is_script, plain_text, priority


class Zenodo:
    def __init__(self, client, sanitizer, announce=print):
        self.client = client
        self.sanitizer = sanitizer
        self.announce = announce

    def record(self, identifier):
        identifier = str(identifier).strip().rstrip('/')
        if not identifier.isascii() or not identifier.isdigit():
            raise MinerError('Zenodo 详情查询需要数字记录标识。')
        return self.client.get_json('/records/' + identifier)

    def candidates(self, queries):
        seen = set()
        for query in queries:
            self.announce("检索 Zenodo（查询内容不写入日志）。")
            page = 1
            while True:
                payload = self.client.get_json("/records", {"q": query, "size": 100, "page": page, "sort": "mostrecent"})
                hits = payload.get("hits", {}).get("hits") if isinstance(payload, dict) else None
                if not isinstance(hits, list):
                    raise MinerError("Zenodo 检索结果格式异常。")
                for record in hits:
                    if not isinstance(record, dict) or "id" not in record:
                        raise MinerError("Zenodo 记录缺少标识。")
                    identifier = str(record["id"])
                    if identifier not in seen:
                        seen.add(identifier)
                        yield self.candidate(record)
                if len(hits) < 100:
                    break
                page += 1

    def candidate(self, record: dict) -> Candidate:
        identifier = str(record["id"])
        if not identifier.isdigit():
            raise MinerError("Zenodo 返回无效记录标识。")
        metadata = record.get("metadata", {})
        files = []
        for item in record.get("files", []):
            links = item.get("links", {})
            files.append(FileEntry(str(item.get("key", "")), str(item.get("checksum", "")),
                                   int(item.get("size", 0)), links.get("content") or links.get("self") or ""))
        # Metadata edits matter even when the deposited record version is unchanged.
        version_input = {"updated": record.get("updated"), "metadata": metadata,
                         "files": [(entry.path, entry.sha, entry.size) for entry in files]}
        version = hashlib.sha256(json.dumps(version_input, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        extra = {"doi": metadata.get("doi"), "keywords": metadata.get("keywords", []),
                 "license": metadata.get("license"), "related_identifiers": metadata.get("related_identifiers", [])}
        return Candidate("zenodo", identifier, version, self.sanitizer.clean(metadata.get("title", "Zenodo " + identifier)),
                         "https://zenodo.org/records/" + quote(identifier, safe=""),
                         self.sanitizer.clean(metadata.get("description", "")), extra, files)

    def evidence(self, candidate: Candidate) -> dict:
        builder = EvidenceBuilder(candidate, self.sanitizer)
        has_layout = builder.inspect_tree(candidate.files)
        builder.notes.append("Zenodo 仅检查公开文件清单和小型文本；未展开压缩包、全文或关联链接。")
        from .links import external_targets
        links = external_targets(candidate.description + "\n" + json.dumps(candidate.metadata, ensure_ascii=False))
        selected = 0
        for entry in sorted(candidate.files, key=priority):
            if not (is_script(entry.path) or is_readme(entry.path)):
                continue
            if entry.size > MAX_SOURCE_BYTES:
                builder.mark_incomplete(entry.path + " 超过 8 MiB 安全读取上限，未检查。")
                continue
            if not entry.download_url:
                raise MinerError("Zenodo 文本附件缺少下载入口；未标记完成。")
            raw = self.client.get_text(entry.download_url, max_bytes=MAX_SOURCE_BYTES)
            try:
                text = plain_text(entry.path, raw)
                export = builder.inspect_text(entry, text) if is_script(entry.path) else False
            except MinerError:
                builder.mark_incomplete(entry.path + " 的 Notebook 代码结构无效，未使用原始输出。")
                continue
            if is_readme(entry.path):
                links.extend(external_targets(text))
            if selected < 40:
                builder.add_text_file(entry, text)
                selected += 1
                if entry.size > MAX_TEXT_BYTES:
                    builder.notes.append(entry.path + "：大文本已全量静态扫描；Notebook 仅保留 code 源码。")
            if selected >= 40:
                builder.mark_incomplete("文本附件已采样 40 个；其余尚未检查。")
                break
        result = builder.build()
        from .physical import hosted_links
        from .evidence import is_layout
        result['external_hosts'] = hosted_links(candidate.description + '\n' + json.dumps(candidate.metadata, ensure_ascii=False))
        result['pointers'] = [{'kind': 'zenodo_file', 'path': f.path, 'url': f.download_url, 'size': f.size}
                              for f in candidate.files if is_layout(f.path)] + result['external_hosts']
        result['physical_layout_confirmed'] = any(is_layout(f.path) and f.size > 0 for f in candidate.files)
        result['script_paths'] = [label for label, _ in builder.sections if is_script(label)]
        result["external_targets"] = sorted(set(links))
        return result
