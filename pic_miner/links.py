"""Extract only allowlisted public identifiers; never request arbitrary external URLs."""

import html
import re
from urllib.parse import unquote, urlsplit

from .github import REPOSITORY_PATTERN

URL_PATTERN = re.compile(r'https?://[^\s<>"\x27]+', re.I)
RESERVED = {'about', 'features', 'topics', 'collections', 'marketplace', 'settings', 'login', 'signup', 'search', 'sponsors', 'orgs', 'users'}


def external_targets(text):
    result = set()
    for match in URL_PATTERN.finditer(html.unescape(text)):
        url = match.group().rstrip('.,;:)]}')
        try:
            parts = urlsplit(url)
            if parts.username or parts.password or parts.port not in (None, 443, 80):
                continue
            path = unquote(parts.path).strip('/').split('/')
            host = (parts.hostname or '').lower()
        except ValueError:
            continue
        if host in {'github.com', 'www.github.com'} and len(path) >= 2 and path[0].lower() not in RESERVED:
            name = '/'.join(path[:2]).removesuffix('.git')
            if REPOSITORY_PATTERN.fullmatch(name) and name.split('/')[1] not in {'', '.', '..'}:
                result.add(('github', name))
        elif host in {'zenodo.org', 'www.zenodo.org'} and len(path) >= 2 and path[0] in {'record', 'records'} and path[1].isdigit():
            result.add(('zenodo', path[1]))
        elif host == 'doi.org':
            match_doi = re.fullmatch(r'10\.5281/zenodo\.(\d+)', '/'.join(path))
            if match_doi:
                result.add(('zenodo', match_doi[1]))
    for identifier in re.findall(r'(?<![\w.])10\.5281/zenodo\.(\d+)', text):
        result.add(('zenodo', identifier))
    return sorted(result)


def target_job(state, source, identifier):
    if source == 'github':
        resolved = state.alias('github', identifier)
        item_id = resolved or 'name:' + identifier.lower()
        existing = state.task('github', item_id)
        if existing:
            import json
            return ('github', item_id, json.loads(existing['payload_json']), existing['hint'])
        return ('github', item_id, {'full_name': identifier}, 'link')
    identifier = state.alias('zenodo', identifier) or identifier
    existing = state.task('zenodo', identifier)
    if existing:
        import json
        return ('zenodo', identifier, json.loads(existing['payload_json']), existing['hint'])
    return ('zenodo', identifier, {'id': identifier}, 'link')
