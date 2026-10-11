"""One bounded discovery page per tick, with durable cursors and source isolation."""

import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import quote

from .config import MinerError
from .github import ORGANIZATION_PATTERN, REPOSITORY_PATTERN
from .links import external_targets, target_job
from .seeds import normalize_seeds

PAGE_SIZE = 100
EPOCH_ARXIV_MINUTE = int(datetime(1991, 1, 1, tzinfo=timezone.utc).timestamp()) // 60
ARXIV_WINDOW_RESULTS = 1000  # Refine large result sets instead of unbounded API offsets.
EPOCH_GITHUB = 1199145600  # Public repository creation range begins in 2008.


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def key(kind, value):
    return kind + ':' + hashlib.sha256(value.encode()).hexdigest()[:24]


def repo_job(repo):
    if not isinstance(repo, dict) or type(repo.get('id')) is not int or not REPOSITORY_PATTERN.fullmatch(str(repo.get('full_name', ''))):
        raise MinerError('GitHub 发现结果缺少合法仓库 ID/名称；游标未前移。')
    # pushed_at is only a queue hint. Actual commit SHA always gates analysis.
    return ('github', str(repo['id']), repo, str(repo.get('pushed_at') or ''))


class Discovery:
    def __init__(self, state, github, zenodo, arxiv, announce, interval=2.5, overlap=600):
        self.state = state
        self.github = github
        self.zenodo = zenodo
        self.arxiv = arxiv
        self.announce = announce
        self.interval = max(interval, 2.5)
        self.overlap = overlap

    def configure(self, seeds):
        seeds = normalize_seeds(seeds)
        self.state.disable_cursors()
        for org in seeds.get('organizations', []):
            if not ORGANIZATION_PATTERN.fullmatch(org):
                raise MinerError('组织种子格式无效。')
            self.state.add_cursor('github', key('organization', org), {'kind': 'organization', 'value': org, 'page': 1})
        for name in seeds.get('repositories', []):
            if not REPOSITORY_PATTERN.fullmatch(name):
                raise MinerError('仓库种子必须为 owner/repository。')
            self.state.enqueue(*target_job(self.state, 'github', name))
        for name in seeds.get('topology_repositories', []):
            if not REPOSITORY_PATTERN.fullmatch(name):
                raise MinerError('拓扑种子必须为 owner/repository。')
            self.state.enqueue(*target_job(self.state, 'github', name))
            self.state.add_cursor('github', key('forks', name), {'kind': 'forks', 'value': name, 'page': 1})
            downstream = '"' + name + '" in:readme'
            self._search_cursor(downstream, history=True, kind='downstream')
            self._search_cursor(downstream, history=False, kind='downstream')
        for query in seeds.get('github_queries', []):
            self._search_cursor(query, history=True)
            self._search_cursor(query, history=False)
        for query in seeds.get('arxiv_queries', []):
            for lane in ('history', 'updates'):
                watermark = stamp(time.time() - 86400) if lane == 'updates' else ''
                self.state.add_cursor('arxiv', key('search-' + lane, query), {
                    'kind': 'search', 'lane': lane, 'query': query, 'start': 0, 'watermark': watermark, 'top': ''})
        for query in seeds.get('zenodo_queries', []):
            self.state.add_cursor('zenodo', key('search', query), {'kind': 'search', 'query': query, 'page': 1, 'watermark': 0})
        self.announce('[SEEDS] GitHub 查询 ' + str(len(seeds['github_queries'])) +
                      '；arXiv 查询 ' + str(len(seeds['arxiv_queries'])) +
                      '；各查询历史/增量游标独立恢复，按来源公平轮转。')

    def _search_cursor(self, query, history, kind='search'):
        lane = 'history' if history else 'updates'
        self.state.add_cursor('github', key(kind + '-' + lane, query), {
            'kind': 'search', 'query': query, 'lane': lane, 'page': 1, 'windows': [],
            'watermark': 0, 'cycle_end': 0})

    def tick(self, row):
        value = json.loads(row['state_json'])
        label = str(value.get('query', value.get('value', '')))[:120]
        self.announce('[' + row['source'] + '] [DISCOVER] ' + value['kind'] +
                      '/' + value.get('lane', 'topology') + '；' + label +
                      '；页码/偏移 ' + str(value.get('page', value.get('start', 0))))
        if row['source'] == 'github':
            jobs, delay = self._github(value)
        elif row['source'] == 'arxiv':
            jobs, delay = self._arxiv(value)
        elif row['source'] == 'zenodo':
            jobs, delay = self._zenodo(value)
        else:
            raise MinerError('未知发现来源。')
        # No writes above: a malformed page or request failure cannot skip unqueued items.
        self.state.page_commit(row, value, jobs, time.time() + delay)
        self.announce('[' + row['source'] + '] [CURSOR] 已持久化 ' + str(len(jobs)) + ' 个发现项；' +
                      ('历史覆盖完成；转向其他查询与增量窗口' if value.get('history_complete')
                       else '本游标下次可用 ' + str(round(delay, 2)) + ' 秒后；其他查询/模型继续'))

    def _github(self, value):
        kind = value['kind']
        if kind in {'organization', 'forks'}:
            name = value['value']
            path = ('/orgs/' + quote(name, safe='') + '/repos' if kind == 'organization'
                    else '/repos/' + quote(name, safe='/') + '/forks')
            params = {'per_page': PAGE_SIZE, 'page': value['page']}
            params.update({'type': 'public', 'sort': 'updated', 'direction': 'desc'} if kind == 'organization' else {'sort': 'newest'})
            payload = self.github.client.get_json(path, params)
            if not isinstance(payload, list):
                raise MinerError('GitHub 列表格式异常。')
            jobs = [repo_job(repo) for repo in payload]
            if len(payload) < PAGE_SIZE:
                value['page'] = 1
                return jobs, self.interval
            value['page'] += 1
            return jobs, .01
        if not value['windows']:
            end = int(time.time())
            if value['lane'] == 'history' and value.get('history_complete'):
                return [], self.interval
            lower = (EPOCH_GITHUB if value['lane'] == 'history' else
                     max(EPOCH_GITHUB, int(value['watermark'] or (end - 86400)) - self.overlap))
            value['windows'] = [[lower, end]]
            value['cycle_end'] = end
            value['page'] = 1
        start, end = value['windows'][0]
        query = value['query']
        if 'in:' not in query:
            query += ' in:name,description,readme'
        if 'fork:' not in query:
            query += ' fork:true'
        query += ' is:public'
        field = 'created' if value['lane'] == 'history' else 'pushed'
        query += ' ' + field + ':' + stamp(start) + '..' + stamp(end)
        payload = self.github.client.get_json('/search/repositories', {
            'q': query, 'sort': 'updated', 'order': 'desc', 'per_page': PAGE_SIZE, 'page': value['page']}, cache=False)
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list) or type(payload.get('total_count')) is not int:
            raise MinerError('GitHub 搜索结果格式异常。')
        if payload.get('incomplete_results'):
            raise MinerError('GitHub 搜索超时/结果不完整；保留窗口稍后重试。')
        total = payload['total_count']
        if total > 1000:
            if end <= start:
                raise MinerError('GitHub 单秒窗口仍超过 1000 条；需细分关键词，当前窗口保留。')
            middle = (start + end) // 2
            # Inclusive overlap plus indexed dedup avoids timestamp boundary omissions.
            value['windows'][:1] = [[middle + 1, end], [start, middle]]
            value['page'] = 1
            self.announce('[github] [CURSOR] 超过搜索 1000 条上限，拆分时间窗口。')
            return [], .01
        jobs = [repo_job(repo) for repo in payload['items']]
        if len(payload['items']) < PAGE_SIZE or value['page'] * PAGE_SIZE >= total:
            value['windows'].pop(0)
            value['page'] = 1
            if not value['windows']:
                value['watermark'] = value['cycle_end']
                if value['lane'] == 'history':
                    value['history_complete'] = True
                    return jobs, self.interval
                return jobs, self.interval
        else:
            value['page'] += 1
        return jobs, .01

    def _arxiv(self, value):
        if value.get('lane') == 'history':
            return self._arxiv_history(value)
        entries, total = self.arxiv.search(value['query'], value['start'], 50)
        jobs = []
        floor = ''
        if value.get('watermark'):
            dt = datetime.fromisoformat(value['watermark'].replace('Z', '+00:00'))
            floor = stamp(dt.timestamp() - self.overlap)
        crossed = False
        for record in entries:
            updated = record['updated']
            if not isinstance(updated, str) or not updated:
                raise MinerError('arXiv 缺少更新时间。')
            # Normalize for lexicographic comparison and validate timestamp syntax.
            updated = stamp(datetime.fromisoformat(updated.replace('Z', '+00:00')).timestamp())
            value['top'] = max(value.get('top', ''), updated)
            if floor and updated < floor:
                crossed = True
                break
            candidate = self.arxiv.candidate(record)
            jobs.append(('arxiv', candidate.resource_id, record, candidate.version))
            # Source workers persist external links independently of the model verdict.
        if crossed or len(entries) < 50 or value['start'] + len(entries) >= total:
            value['watermark'] = max(value.get('watermark', ''), value['top'])
            value['top'] = ''
            value['start'] = 0
            return jobs, max(3.5, self.interval)
        value['start'] += len(entries)
        return jobs, .01

    def _arxiv_history(self, value):
        """Persistent, bounded submitted-date windows; never use an update watermark as a floor."""
        delay = max(3.5, self.interval)
        if value.get('history_complete'):
            return [], delay
        if not value.get('windowed_history'):
            # Old history cursors behaved like updates after their first sweep.
            # Re-anchor once; the task primary key prevents duplicate inference.
            value.update(windowed_history=True, windows=[], start=0, top='')
        if not value.get('windows'):
            end = int(time.time()) // 60
            value['windows'] = [[EPOCH_ARXIV_MINUTE, end]]
            value['history_end'] = end
            value['start'] = 0
        lower, upper = value['windows'][0]
        def date(minute):
            return datetime.fromtimestamp(minute * 60, timezone.utc).strftime('%Y%m%d%H%M')
        query = '(' + value['query'] + ') AND submittedDate:[' + date(lower) + ' TO ' + date(upper) + ']'
        entries, total = self.arxiv.search(query, value['start'], 50, sort_by='submittedDate')
        if total > ARXIV_WINDOW_RESULTS and lower < upper:
            middle = (lower + upper) // 2
            value['windows'][:1] = [[middle + 1, upper], [lower, middle]]
            value['start'] = 0
            self.announce('[arxiv] [CURSOR] 大结果集按提交日期二分；优先近期，再推进历史。')
            return [], .01
        if total > 30000:
            raise MinerError('arXiv 单分钟窗口超过 API 深分页范围；请细分查询，当前窗口保留。')
        if not entries and value['start'] < total:
            raise MinerError('arXiv 返回空页但窗口仍有结果；游标保留，稍后重试。')
        jobs = []
        for record in entries:
            updated = record.get('updated')
            if not isinstance(updated, str) or not updated:
                raise MinerError('arXiv 缺少更新时间。')
            stamp(datetime.fromisoformat(updated.replace('Z', '+00:00')).timestamp())
            candidate = self.arxiv.candidate(record)
            jobs.append(('arxiv', candidate.resource_id, record, candidate.version))
        if value['start'] + len(entries) >= total:
            value['windows'].pop(0)
            value['start'] = 0
            if not value['windows']:
                value['history_complete'] = True
                value['watermark'] = stamp(value['history_end'] * 60)
                return jobs, delay
        else:
            value['start'] += len(entries)
        return jobs, .01

    def _zenodo(self, value):
        if not value.get('cycle_end'):
            value['cycle_end'] = int(time.time())
        lower = max(0, int(value['watermark']) - self.overlap)
        query = '(' + value['query'] + ') AND updated:[' + stamp(lower) + ' TO ' + stamp(value['cycle_end']) + ']'
        payload = self.zenodo.client.get_json('/records', {
            'q': query, 'size': PAGE_SIZE, 'page': value['page'], 'sort': 'mostrecent'}, cache=False)
        hits = payload.get('hits', {}).get('hits') if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            raise MinerError('Zenodo 搜索结果格式异常。')
        jobs = []
        for record in hits:
            candidate = self.zenodo.candidate(record)
            jobs.append(('zenodo', candidate.resource_id, record, candidate.version))
            text = json.dumps(record.get('metadata', {}), ensure_ascii=False)
            jobs.extend(target_job(self.state, source, item) for source, item in external_targets(text))
        if len(hits) < PAGE_SIZE:
            value['watermark'] = value['cycle_end']
            value['cycle_end'] = 0
            value['page'] = 1
            return jobs, self.interval
        value['page'] += 1
        return jobs, .01
