"""GitHub organization/repository enumeration and immutable-commit evidence."""

import base64
import binascii
import re
import hashlib
import json
import requests
from urllib.parse import quote

from .config import APIError, MAX_SOURCE_BYTES, MAX_TEXT_BYTES, MinerError
from .evidence import (Candidate, EvidenceBuilder, FileEntry, is_layout, is_markdown, is_readme,
                       is_script, markdown_slices, optical_matches, plain_text, priority, strong_description)


REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ORGANIZATION_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
MAX_DOCUMENT_FILES = 16
MAX_SCRIPT_FILES = 40


def document_priority(entry):
    parts = entry.path.lower().split('/')
    root_readme = len(parts) == 1 and is_readme(entry.path)
    physical = bool(re.search(r'layout|pdk|ports?|components?|devices?|heater|electrod|metal|routing|export', entry.path, re.I))
    return not root_readme, not physical, *priority(entry)


class GitHub:
    def __init__(self, client, sanitizer, announce=print):
        self.client = client
        self.sanitizer = sanitizer
        self.announce = announce

    def repositories(self, organizations, explicit):
        seen = set()
        for organization in organizations:
            if not isinstance(organization, str) or not ORGANIZATION_PATTERN.fullmatch(organization):
                raise MinerError("组织种子格式无效。")
            self.announce("枚举 GitHub 组织：" + self.sanitizer.clean(organization))
            page = 1
            while True:
                payload = self.client.get_json("/orgs/" + quote(organization, safe="") + "/repos",
                                               {"per_page": 100, "page": page, "type": "public"})
                if not isinstance(payload, list):
                    raise MinerError("GitHub 组织列表格式异常。")
                for repo in payload:
                    if not isinstance(repo, dict) or "id" not in repo or "full_name" not in repo:
                        raise MinerError("GitHub 仓库元数据不完整。")
                    key = str(repo["id"])
                    if key not in seen:
                        seen.add(key)
                        yield repo
                if len(payload) < 100:
                    break
                page += 1
        for name in explicit:
            if not isinstance(name, str) or not REPOSITORY_PATTERN.fullmatch(name):
                raise MinerError("仓库种子必须为 owner/repository。")
            payload = self.client.get_json("/repos/" + quote(name, safe="/"))
            if not isinstance(payload, dict) or "id" not in payload or "full_name" not in payload:
                raise MinerError("GitHub 仓库元数据不完整。")
            key = str(payload["id"])
            if key not in seen:
                seen.add(key)
                yield payload

    def releases(self, name):
        from .physical import layout_asset
        assets = []
        page = 1
        while True:
            rows = self.client.get_json('/repos/' + quote(name, safe='/') + '/releases',
                                        {'per_page': 100, 'page': page})
            if not isinstance(rows, list):
                raise MinerError('GitHub Releases 元数据格式异常。')
            for release in rows:
                if not isinstance(release, dict) or not isinstance(release.get('assets', []), list):
                    raise MinerError('GitHub Release 附件列表格式异常。')
                if release.get('draft'):
                    continue
                items = release.get('assets', [])
                if len(items) >= 30 and release.get('id') is not None:
                    items = []
                    asset_page = 1
                    while True:
                        batch = self.client.get_json('/repos/' + quote(name, safe='/') + '/releases/' +
                                                     str(release['id']) + '/assets', {'per_page': 100, 'page': asset_page})
                        if not isinstance(batch, list):
                            raise MinerError('GitHub Release 附件分页格式异常。')
                        items.extend(batch)
                        if len(batch) < 100:
                            break
                        asset_page += 1
                for asset in items:
                    if not isinstance(asset, dict):
                        raise MinerError('GitHub Release 附件格式异常。')
                    filename = str(asset.get('name', ''))
                    if layout_asset(filename):
                        assets.append({'kind': 'release_asset', 'path': filename,
                                       'size': int(asset.get('size', 0)), 'id': asset.get('id'),
                                       'url': str(asset.get('browser_download_url') or release.get('html_url') or ''),
                                       'updated': asset.get('updated_at') or release.get('published_at'),
                                       'digest': asset.get('digest'), 'tag': release.get('tag_name')})
            if len(rows) < 100:
                return sorted(assets, key=lambda a: (str(a.get('id')), a['path'], str(a.get('tag'))))
            page += 1

    def candidate(self, repo: dict):
        name = repo['full_name']
        if not isinstance(name, str) or not REPOSITORY_PATTERN.fullmatch(name):
            raise MinerError('GitHub 返回无效仓库名。')
        assets = self.releases(name)
        branch = repo.get('default_branch')
        sha = None
        if isinstance(branch, str) and branch:
            try:
                reference = self.client.get_json('/repos/' + quote(name, safe='/') + '/git/ref/heads/' + quote(branch, safe='/'))
                obj = reference.get('object') if isinstance(reference, dict) else None
                sha = obj.get('sha') if isinstance(obj, dict) and obj.get('type') == 'commit' else None
                if not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{40,64}', sha):
                    raise MinerError('GitHub 返回无效 commit SHA。')
            except APIError as error:
                if error.status != 409 and not (error.status == 404 and repo.get('size') == 0):
                    raise
        elif repo.get('size') != 0:
            raise MinerError('GitHub 仓库缺少默认分支。')
        version = sha or 'empty'
        if assets:
            version += ':' + hashlib.sha256(json.dumps(assets, sort_keys=True).encode()).hexdigest()[:24]
        version += ':meta:' + hashlib.sha256(json.dumps({
            'description': repo.get('description'), 'topics': repo.get('topics'),
            'license': repo.get('license'), 'default_branch': branch}, sort_keys=True).encode()).hexdigest()[:16]
        metadata = {'default_branch': branch, 'topics': repo.get('topics', []),
                    'archived': bool(repo.get('archived')), 'fork': bool(repo.get('fork')),
                    'pushed_at': repo.get('pushed_at'), 'commit_sha': sha, 'no_commit': sha is None,
                    'release_assets': assets, 'license': (repo.get('license') or {}).get('spdx_id')}
        return Candidate('github', str(repo['id']), version, self.sanitizer.clean(name),
                         'https://github.com/' + quote(name, safe='/'),
                         self.sanitizer.clean(repo.get('description') or ''), metadata)

    def files(self, candidate: Candidate):
        if candidate.metadata.get("no_commit"):
            return [], 0
        head = candidate.metadata.get("commit_sha") or candidate.version
        prefix = "/repos/" + quote(candidate.name, safe="/") + "/git/trees/"
        tree = self.client.get_json(prefix + head, {"recursive": "1"})
        if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
            raise MinerError("GitHub 文件树格式异常。")
        if not tree.get("truncated"):
            rows = [(item.get("path", ""), item) for item in tree["tree"]]
        else:
            # Never interpret a truncated recursive result as a complete tree.
            rows = []
            stack = [("", head)]
            visits = 0
            while stack:
                directory, sha = stack.pop()
                subtree = self.client.get_json(prefix + sha)
                if not isinstance(subtree, dict) or subtree.get("truncated") or not isinstance(subtree.get("tree"), list):
                    raise MinerError("GitHub 子目录树不完整；保留断点，下次重试。")
                for item in subtree["tree"]:
                    path = directory + item.get("path", "")
                    if item.get("type") == "tree":
                        stack.append((path + "/", item["sha"]))
                    else:
                        rows.append((path, item))
                visits += 1
                if visits > 20_000:
                    raise MinerError("GitHub 文件树超过本次安全遍历上限；未标记完成。")
        entries = []
        submodules = 0
        for path, item in rows:
            if item.get("type") == "blob":
                entries.append(FileEntry(path, item.get("sha", ""), int(item.get("size", 0))))
            elif item.get("type") == "commit":
                submodules += 1
        return entries, submodules

    def blob_bytes(self, candidate: Candidate, entry: FileEntry) -> bytes:
        path = "/repos/" + quote(candidate.name, safe="/") + "/git/blobs/" + entry.sha
        payload = self.client.get_json(path)
        if not isinstance(payload, dict) or payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise MinerError("GitHub 文本 blob 格式异常。")
        try:
            raw = base64.b64decode(payload["content"], validate=False)
        except (ValueError, binascii.Error):
            raise MinerError("GitHub blob 编码异常。") from None
        if len(raw) > MAX_SOURCE_BYTES:
            raise MinerError("GitHub 源文件超过 8 MiB 安全读取上限。")
        # Decode the full bounded source before cleaning/ranking: late calls matter.
        return raw

    def text(self, candidate: Candidate, entry: FileEntry) -> str:
        return self.blob_bytes(candidate, entry).decode("utf-8", errors="replace")

    def evidence(self, candidate: Candidate) -> dict:
        files, submodules = self.files(candidate)
        candidate.files = files
        builder = EvidenceBuilder(candidate, self.sanitizer)
        has_layout = builder.inspect_tree(files)
        from .links import external_targets
        links, documents = [], []
        document_files = sorted((entry for entry in files if is_readme(entry.path) or
                                 is_markdown(entry.path) or entry.path.lower().endswith('.rst')), key=document_priority)
        for entry in document_files[:MAX_DOCUMENT_FILES]:
            if entry.size > MAX_SOURCE_BYTES:
                builder.mark_incomplete(entry.path + ' 超过 8 MiB 文档读取上限。')
                continue
            try:
                raw = self.text(candidate, entry)
            except (MinerError, requests.RequestException, ValueError, TypeError, KeyError):
                builder.mark_incomplete(entry.path + ' 文档读取失败；继续其他文档。')
                continue
            documents.append((entry.path, raw))
            builder.preserve(entry.path, raw)
            builder.optical_keywords.update(optical_matches(raw))
            builder.descriptive_match = builder.descriptive_match or strong_description(builder.description_context + '\n' + raw)
            links.extend(external_targets(raw))
        if len(document_files) > MAX_DOCUMENT_FILES:
            builder.mark_incomplete('Markdown 文档按路径/器件优先级采样 ' + str(MAX_DOCUMENT_FILES) + ' 个文件。')
        document_text = markdown_slices(documents, 3000)
        if document_text:
            builder.add('Markdown documentation (ranked paragraphs; <=3000 characters)', document_text)
        from .physical import LAYOUT_NAME, attribute_rules, binary_layout, hosted_links, layout_asset, lfs_pointer, tracked_lfs
        physical = False
        pointers, lfs_objects, external_hosts = [], [], []
        external_hosts = hosted_links(candidate.description)
        for _, raw in documents:
            external_hosts.extend(hosted_links(raw))
        pointers.extend(external_hosts)
        if any(h['layout_claim'] for h in external_hosts):
            builder.matched = True
            builder.rules.append('external_hosted_layout')
            builder.add('external hosted layout clues', json.dumps(external_hosts, ensure_ascii=False))
        if not files and not candidate.metadata.get('release_assets') and not external_hosts:
            builder.descriptive_match = False
            builder.notes.append('文件树为空且无附件/外部托管入口；不把描述宣传当成版图。')
        assets = candidate.metadata.get('release_assets', [])
        for asset in assets:
            builder.add('Release physical layout asset', json.dumps(asset, ensure_ascii=False))
            pointers.append(asset)
            builder.matched = True
            if 'release_asset' not in builder.rules:
                builder.rules.append('release_asset')
            physical = physical or (is_layout(asset['path']) and asset['size'] > 0)
        rules = []
        for entry in sorted(files, key=lambda e: (e.path.count('/'), e.path)):
            if entry.path.endswith('.gitattributes') and entry.size <= MAX_SOURCE_BYTES:
                raw = self.text(candidate, entry)
                rules.extend(attribute_rules(entry.path, raw))
                builder.add_file(entry, raw)
        for entry in files:
            if not (layout_asset(entry.path) or (tracked_lfs(entry.path, rules) and LAYOUT_NAME.search(entry.path))):
                continue
            pointer = {'kind': 'repository_file', 'path': entry.path,
                       'url': candidate.url + '/blob/' + quote(candidate.metadata.get('commit_sha') or candidate.version, safe='') + '/' + quote(entry.path, safe='/'),
                       'size': entry.size}
            if layout_asset(entry.path) and not is_layout(entry.path):
                builder.matched = True
                if 'layout_archive' not in builder.rules:
                    builder.rules.append('layout_archive')
            confirmed = entry.size > 4096 and is_layout(entry.path)
            if 0 < entry.size <= 4096:
                raw = self.blob_bytes(candidate, entry)
                lfs = lfs_pointer(raw)
                if lfs:
                    pointer.update(lfs)
                    pointer['kind'] = 'lfs_pointer'
                    lfs_objects.append(pointer.copy())
                    builder.add('Git LFS physical layout pointer', json.dumps(pointer, ensure_ascii=False))
                    builder.rules.append('lfs_layout_object')
                    builder.matched = True
                    confirmed = is_layout(entry.path)
                else:
                    confirmed = binary_layout(raw)
                    builder.add('small layout inspection', entry.path + ': ' + ('binary layout header' if confirmed else 'no verified binary header; filename clue only'))
            physical = physical or confirmed
            pointers.append(pointer)
        if candidate.metadata.get('no_commit'):
            builder.descriptive_match = False
            builder.notes.append('仓库没有 commit/文件树；描述中的制造宣传不证明实体，只保留实际附件证据。')
        builder.notes.append('固定 HEAD 静态检查并枚举 Releases；未展开附件/压缩包、其他分支或子模块。')
        if submodules:
            builder.notes.append("存在 " + str(submodules) + " 个子模块，未递归检查。")
        scripts = sorted((entry for entry in files if is_script(entry.path)), key=priority)
        selected = 0
        for position, entry in enumerate(scripts):
            if position >= MAX_SCRIPT_FILES:
                builder.mark_incomplete('大仓脚本已按优先级检查 ' + str(MAX_SCRIPT_FILES) + ' 个；其余保留文件路径线索。')
                break
            if entry.size > MAX_SOURCE_BYTES:
                builder.mark_incomplete(entry.path + " 超过 8 MiB 安全读取上限，未检查。")
                continue
            try:
                raw = self.text(candidate, entry)
                text = plain_text(entry.path, raw)
                export = builder.inspect_text(entry, text)
            except (MinerError, requests.RequestException, ValueError, TypeError, KeyError):
                builder.mark_incomplete(entry.path + " 源码读取/Notebook 解析失败；继续其余文件，未执行代码。")
                continue
            builder.add_text_file(entry, text)
            selected += 1
            if entry.size > MAX_TEXT_BYTES:
                builder.notes.append(entry.path + "：大文本已全量静态扫描；Notebook 仅保留 code 源码。")
        result = builder.build()
        result['physical_layout_confirmed'] = physical or any(
            re.search(r'(?<!def )\b(?:write_gds|write_oas|export_gds|to_gds)\s*\(', text)
            for label, text in builder.sections if is_script(label))
        result['pointers'] = pointers
        result['lfs_objects'] = lfs_objects
        result['external_hosts'] = external_hosts
        result['script_paths'] = [label for label, _ in builder.sections if is_script(label)]
        result['document_paths'] = [path for path, _ in documents]
        result['document_chars'] = len(document_text)
        result["external_targets"] = sorted(set(links))
        return result
