"""Validate keyword groups and expand them fairly into the existing seed contract."""
from itertools import zip_longest

from .config import MinerError

SEED_FIELDS = ('organizations', 'repositories', 'topology_repositories',
               'github_queries', 'arxiv_queries', 'zenodo_queries')
GROUP_FIELDS = ('github_query_groups', 'arxiv_query_groups')


def normalize_seeds(content, error_type=MinerError):
    if not isinstance(content, dict) or set(content) - set(SEED_FIELDS + GROUP_FIELDS):
        raise error_type('种子配置只支持：' + '、'.join(SEED_FIELDS + GROUP_FIELDS) + '。')
    result = {}
    for field in SEED_FIELDS:
        items = content.get(field, [])
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise error_type('种子配置的各项必须是非空字符串列表。')
        result[field] = [item.strip() for item in items]
    for field in GROUP_FIELDS:
        groups = content.get(field, {})
        if not isinstance(groups, dict):
            raise error_type(field + ' 必须是组名到关键词列表的对象。')
        for name, items in groups.items():
            if (not isinstance(name, str) or not name.strip() or not isinstance(items, list)
                    or not items or not all(isinstance(item, str) and item.strip() for item in items)):
                raise error_type(field + ' 的组名和关键词列表必须非空且类型正确。')
        # Alternate groups before adding a group's second/third keyword.
        expanded = [item.strip() for row in zip_longest(*groups.values()) for item in row if item is not None]
        result[field.replace('_query_groups', '_queries')].extend(expanded)
    return {field: list(dict.fromkeys(items)) for field, items in result.items()}
