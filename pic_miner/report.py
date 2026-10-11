"""Atomic compact dashboard derived from durable, latest-per-resource report entries."""

import hashlib
import html
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

from .config import MinerError
from .judge import LEVEL_1, LEVEL_2
from .state import REVIEW
from .scope import REASONS

START = '<!-- pic-dashboard:start -->'
END = '<!-- pic-dashboard:end -->'
LAYOUT_VERSION = '<!-- pic-dashboard:layout-v4 -->'


def markdown(value):
    text = html.escape(str(value), quote=False)
    for char in '\\`*_[]':
        text = text.replace(char, '\\' + char)
    return ' '.join(text.split())


def safe_url(value):
    try:
        parts = urlsplit(str(value))
        if parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.password:
            return ''
        return quote(str(value), safe=':/?=&%#@+,-._~')
    except ValueError:
        return ''


class Reporter:
    def __init__(self, state, path: Path, sanitizer, output=print, color=None):
        self.state, self.path, self.sanitizer, self.output = state, path, sanitizer, output
        self.color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ if color is None else color
        self.state.migrate_reports()
        removed = self.state.audit_optical_reports()
        if removed:
            self.output('[OPTICAL AUDIT] 旧看板移除 ' + str(removed) + ' 条无光学证据的候选；历史扫描保留，已存在的任务排队重检。')
        self.state.migrate_review_reports()
        self._layout_checked = False

    def flush(self):
        pending = self.state.pending_hits()
        revision = self.state.dashboard_revision()
        dirty = revision['revision'] != revision['published']
        outdated_layout = not self._layout_checked and self._outdated_layout()
        if dirty or pending or outdated_layout or (not self.path.exists() and self.state.dashboard_rows()):
            self._publish(revision['revision'])
        self._layout_checked = True
        for row in pending:
            self.deliver(row)
        return len(pending)

    def _outdated_layout(self):
        """Upgrade an already published board even when its database revision is clean."""
        if not self.path.exists():
            return False
        try:
            text = self.path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            raise MinerError('hits.md 看板读取失败；SQLite 中的命中与刷新状态已保留。') from None
        return START in text and LAYOUT_VERSION not in text

    def _notes(self, existing):
        if START in existing and END in existing:
            before, rest = existing.split(START, 1)
            _, after = rest.split(END, 1)
            return (before + after).strip()
        if '<!-- pic-hit:' in existing:
            # Old generated raw evidence is retained in SQLite and the migration backup.
            prefix = re.split(r'(?m)^## ', existing, maxsplit=1)[0]
            return prefix.replace('# PIC 版图候选', '').strip()
        return existing.strip()

    def _publish(self, revision):
        try:
            existing = self.path.read_text(encoding='utf-8') if self.path.exists() else ''
            notes = self._notes(existing)
            rows = self.state.dashboard_rows()
            metrics = self.state.funnel_metrics()
            # Negative-only runs still publish the filtering funnel.
            if not rows and not existing and not metrics:
                self.state.mark_dashboard_published(revision)
                return
            updated = self.state.dashboard_updated_at() or max((row['updated_at'] for row in rows), default='—')
            n1 = sum(row['level'] == LEVEL_1 for row in rows)
            n2 = sum(row['level'] == LEVEL_2 for row in rows)
            reviews = [row for row in rows if row['level'] == REVIEW]
            lines = [START, LAYOUT_VERSION, '# PIC 资源实时看板', '', '## 【实时统计看板】', '',
                     '| 总命中数 | Level 1 | Level 2 | 待人工核对 (Review) | 最近更新时间（UTC） |', '|---:|---:|---:|---:|---|',
                     '| ' + str(n1 + n2) + ' | ' + str(n1) + ' | ' + str(n2) + ' | ' + str(len(reviews)) + ' | ' + updated + ' |', '']
            lines.extend(self._render_funnel(metrics))
            sections = ((LEVEL_1, '## 🌟 Level 1: 确定性物理版图/流片实体'),
                        (LEVEL_2, '## 🔍 Level 2: 高价值候选与工具/版图线索'),
                        (REVIEW, '## ⚠️ 待核对核心线索 (Pending Review)'))
            for level, heading in sections:
                lines.extend([heading, ''])
                group = [row for row in rows if row['level'] == level]
                lines.extend(self.render(row, number) for number, row in enumerate(group, start=1))
                if level == REVIEW and not group:
                    lines.extend(['暂无待核对条目。', ''])
            lines.extend([END, ''])
            text = '\n'.join(lines)
            if notes:
                text += '\n' + notes + '\n'
            self._atomic_write(self.sanitizer.clean(text))
            self.state.mark_dashboard_published(revision)
        except (OSError, UnicodeError):
            raise MinerError('hits.md 看板写入失败；SQLite 中的命中与刷新状态已保留。') from None

    def _render_funnel(self, metrics):
        value = lambda name: metrics.get(name, 0)
        filtered = sum(count for key, count in metrics.items() if key.startswith('outcome:filtered:'))
        review = sum(count for key, count in metrics.items() if key.startswith('outcome:review:'))
        failed = sum(count for key, count in metrics.items() if key.startswith('outcome:error:'))
        hits = value('outcome:level1') + value('outcome:level2')
        lines = ['### 扫描与过滤漏斗', '', '| 扫描资源总量（去重） | 已命中（L1+L2） | 已过滤 | 待核对 | 取证/模型异常 | 首次研判中 |',
                 '|---:|---:|---:|---:|---:|---:|',
                 '| ' + ' | '.join(str(v) for v in (value('resources'), hits, filtered, review, failed, value('outcome:reading'))) + ' |', '',
                 '| 处理事件（升级后累计） | 次数 |', '|---|---:|']
        for name, label in (('analysis_batches', '研判批次（含续读/重评）'),
                            ('skip_unchanged', '版本与策略未变，跳过'), ('skip_review', '已待核对，跳过重复研判'),
                            ('model_requests', '模型请求'), ('quote_validation', '引用校验失败（含重试恢复）'),
                            ('output_validation', 'JSON/字段校验失败（含重试恢复）'),
                            ('model_unavailable', '模型请求/完成状态异常'), ('source_errors', '来源取证/解析失败')):
            lines.append('| ' + label + ' | ' + str(value('event:' + name)) + ' |')
        lines.extend(['', '| 当前过滤/待核对/失败原因（资源数） | 数量 |', '|---|---:|'])
        found = False
        for category, label in REASONS.items():
            count = sum(value('outcome:' + prefix + category) for prefix in ('filtered:', 'review:', 'error:'))
            if count:
                lines.append('| ' + label + ' | ' + str(count) + ' |'); found = True
        if not found:
            lines.append('| 暂无过滤或失败记录 | 0 |')
        lines.extend(['', '扫描总量 = 已命中（L1+L2） + 已过滤 + 待核对 + 取证/模型异常 + 首次研判中，各列互斥。资源数按每个来源/资源的最新状态统计，重评和分块不重复计数；事件数自升级起累计。历史缺少原因的记录明确列为未分类。', ''])
        return lines

    def _atomic_write(self, text):
        descriptor, temporary = tempfile.mkstemp(prefix='.hits.', suffix='.tmp', dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def deliver(self, row):
        assessment = json.loads(row['assessment_json'])
        key = (row['source'], row['resource_id'], row['version'], row['pipeline_key'])
        current = self.state.dashboard_item(*key[:2])
        payload = json.loads(current[0]) if current else {}
        if (assessment.get('level') in {LEVEL_1, LEVEL_2} and payload.get('version') == row['version']
                and payload.get('pipeline_key') == row['pipeline_key']):
            text = '[HIT ' + assessment['level'] + '] ' + row['name'] + '\n' + row['url'] + '\n' + assessment['summary']
            text = self.sanitizer.clean(text)
            self.output('\033[1;32m' + text + '\033[0m' if self.color else text)
        self.state.mark_reported(key)

    def _entry_title(self, row, payload, number):
        clean = lambda value: markdown(self.sanitizer.clean(value))
        source = {'github': 'GitHub', 'arxiv': 'arXiv', 'zenodo': 'Zenodo'}.get(row['source'], row['source'])
        label = row['item_id'] if row['source'] == 'arxiv' else payload['name']
        prefix = {LEVEL_1: 'L1', LEVEL_2: 'L2', REVIEW: 'R'}[row['level']]
        return ('### [' + prefix + '-' + str(number).zfill(2) + '] [' + source + '] [' + clean(label) +
                '](' + safe_url(self.sanitizer.clean(payload['url'])) + ')')

    def render(self, row, number=1):
        payload = json.loads(row['payload_json'])
        if row['level'] == REVIEW:
            return self.render_review(row, payload, number)
        assessment, evidence = payload['assessment'], payload['evidence']
        clean = lambda value: markdown(self.sanitizer.clean(value))
        identifier = hashlib.sha256(json.dumps((row['source'], row['item_id'])).encode()).hexdigest()
        title = self._entry_title(row, payload, number)
        features = assessment.get('physical_features', [])
        primary, references = self._file_references(row, payload, evidence)
        references += [clean(q['id'] + ': ' + q['quote'][:200]) for q in assessment.get('evidence', [])]
        fold = '<details><summary>次要路径与引用</summary>' + '<br>'.join(references) + '</details>' if references else ''
        lines = [title + ' <!-- pic-hit:' + identifier + ' -->',
                 '- **核心研判**：' + clean(assessment['summary']),
                 '- **核心物理特征**：' + '；'.join(clean(f) for f in features),
                 '- **版图/指针**：' + ('；'.join(primary) if primary else '正文/README 声明，附件入口待核实'),
                 '- **更新**：' + row['updated_at'] + ' · `' + clean(payload['version'])[:64] + '`' +
                 (' · 历史记录待三档复核' if payload.get('migrated') else '')]
        if fold:
            lines.append(fold)
        lines.append('')
        return '\n'.join(lines) + '\n'

    def _file_references(self, row, payload, evidence):
        clean = lambda value: markdown(self.sanitizer.clean(value))
        pointers = [p.copy() for p in evidence.get('pointers', []) if isinstance(p, dict)]
        known = {str(p.get('path')) for p in pointers if isinstance(p, dict)}
        for path in evidence.get('paths', []) + evidence.get('script_paths', []) + evidence.get('document_paths', []):
            if path not in known:
                pointers.append({'path': path})
                known.add(path)
        primary = []
        for pointer in pointers[:3]:
            path = clean(pointer.get('path') or pointer.get('url') or '外部托管入口')
            if pointer.get('original_size'):
                path += '（LFS 原始 ' + str(pointer['original_size']) + ' bytes）'
            url = safe_url(self.sanitizer.clean(pointer.get('url', '')))
            commit = payload.get('commit_sha') or payload.get('version', '').split(':', 1)[0]
            if not url and row['source'] == 'github' and commit and pointer.get('path'):
                base = safe_url(self.sanitizer.clean(payload.get('url', '')))
                if base:
                    url = base.rstrip('/') + '/blob/' + quote(self.sanitizer.clean(commit), safe='') + '/' + quote(self.sanitizer.clean(pointer['path']), safe='/')
            primary.append('[' + path + '](' + url + ')' if url else path)
        references = [clean(p.get('path') or p.get('url') or '') for p in pointers[3:20]]
        return primary, references

    def render_review(self, row, payload, number=1):
        clean = lambda value: markdown(self.sanitizer.clean(value))
        evidence, assessment = payload['evidence'], payload['assessment']
        identifier = hashlib.sha256(json.dumps((row['source'], row['item_id'])).encode()).hexdigest()
        title = self._entry_title(row, payload, number)
        reason = assessment.get('reason') or '研判未完成或静态覆盖不足，需要人工核对。'
        features = evidence.get('review_features', [])
        primary, references = self._file_references(row, payload, evidence)
        excerpt = evidence.get('review_excerpt', '')
        if excerpt:
            references.append(clean(excerpt))
        lines = [title + ' <!-- pic-review:' + identifier + ' -->',
                 '- **待核对原因**：' + clean(reason),
                 '- **已提取核心特征**：' + ('；'.join(clean(feature) for feature in features) if features else '尚无明确物理特征，已有证据与待核对原因已保留。'),
                 '- **关键文件/入口**：' + ('；'.join(primary) if primary else '从标题链接打开资源，核对 README、文档与附件。'),
                 '- **更新**：' + row['updated_at'] + ' · `' + clean(payload['version'])[:64] + '`']
        if references:
            lines.append('<details><summary>次要路径与静态证据摘要</summary>' + '<br>'.join(references) + '</details>')
        lines.append('')
        return '\n'.join(lines) + '\n'
