"""Bounded, authenticated requests with conservative retries and no URL logging."""

import hashlib
import json
import random
import re
import time
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import requests

from .config import APIError, MAX_TEXT_BYTES, MinerError, ModelUnavailable, ModelRequestRejected, Sanitizer


def origin(url: str) -> tuple:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port


def zenodo_url(url: str, base=False) -> str:
    """Canonical public REST details; leave file download paths and custom hosts intact."""
    parts = urlsplit(url)
    if (parts.hostname in {'zenodo.org', 'www.zenodo.org'} and parts.port in {None, 80, 443}
            and not parts.username and not parts.password and parts.scheme in {'http', 'https'}):
        path = parts.path.rstrip('/')
        if base and path in {'', '/api'}:
            path = '/api'
        detail = re.fullmatch(r'/(?:api/)?records?/(\d+)', path)
        if detail:
            path = '/api/records/' + detail.group(1)
        return urlunsplit(('https', 'zenodo.org', path, parts.query, parts.fragment))
    return url


class HTTPClient:
    def __init__(self, base_url: str, label: str, state=None, token: str = "",
                 timeout: float = 30, min_interval: float = 2.5, max_wait: float = 30,
                 continuous_rate_wait: bool = False, shutdown=None, announce=print, pacer=None, sanitizer=None):
        self.base_url = (zenodo_url(base_url, base=True) if label == 'Zenodo' else base_url).rstrip("/")
        self.label = label
        parts = urlsplit(self.base_url)
        self.error_sanitizer = sanitizer or Sanitizer((self.base_url, parts.netloc, parts.hostname or '', token))
        self.state = state
        self.timeout = timeout
        self.pacer = pacer
        self.min_interval = min_interval
        self.max_wait = max_wait
        self.continuous_rate_wait = continuous_rate_wait
        self.shutdown = shutdown
        self.announce = announce
        self.bucket_waits = {}
        self.search_last = 0.0
        self.last_request = 0.0
        self.not_before = 0.0
        self.session = requests.Session()
        # Do not send LAN model traffic or credentials to an inherited proxy/netrc.
        self.session.trust_env = False
        if label == 'Zenodo':
            self.session.max_redirects = 5
            self.session.hooks['response'].append(self._zenodo_redirect)
        self.session.headers.update({"User-Agent": "pic-resource-miner/1.0",
                                     "Accept": "application/json"})
        if token:
            self.session.headers["Authorization"] = "Bearer " + token
        if label == "GitHub":
            self.session.headers["Accept"] = "application/vnd.github+json"

    def close(self) -> None:
        self.session.close()

    def _url(self, path: str) -> str:
        url = path if path.startswith(("https://", "http://")) else self.base_url + "/" + path.lstrip("/")
        if self.label == 'Zenodo':
            url = zenodo_url(url)
        parts = urlsplit(url)
        if origin(url) != origin(self.base_url) or parts.username or parts.password:
            raise MinerError(self.label + " 拒绝跨来源 URL，防止凭证转发。")
        return url

    def _zenodo_redirect(self, response, **kwargs):
        """Response hooks run BEFORE Requests follows a Location, including every hop."""
        if response.status_code not in {301, 302, 303, 307, 308} or response.request.method != 'GET':
            return response
        location = response.headers.get('Location')
        if not location:
            return response
        try:
            target = urljoin(response.url, location)
            parts = urlsplit(target)
            allowed = origin(target) == origin(self.base_url) and not parts.username and not parts.password
        except ValueError:
            allowed = False
        if not allowed:
            response.close()
            raise APIError('Zenodo', response.status_code)
        # Automatic hops share the ordinary Zenodo request pacing and embargo.
        try:
            self._pace(target)
        except MinerError:
            response.close()
            raise
        return response

    def _wait(self, delay, countdown=False):
        if self.shutdown:
            self.shutdown.wait(delay, self.label + " 频控", countdown=countdown)
        elif countdown:
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                self.announce("[WAIT] " + self.label + " 频控：剩余 " + str(int(remaining + .999)) + " 秒")
                time.sleep(min(1, remaining))
        else:
            time.sleep(delay)

    def _pace(self, url="") -> None:
        bucket = "search" if "/search/" in url else "core"
        delay = max(self.not_before - time.time(),
                    self.bucket_waits.get(bucket, 0) - time.time(),
                    self.min_interval - (time.monotonic() - self.last_request), 0)
        if self.pacer is not None:
            delay = max(delay, self.min_interval - (time.monotonic() - self.pacer.last_request))
        if self.continuous_rate_wait and self.label == "GitHub" and bucket == "search":
            delay = max(delay, 2.5 - (time.monotonic() - self.search_last))
        if delay > self.max_wait and not self.continuous_rate_wait:
            raise MinerError(self.label + " 频控等待超过本次预算；请稍后重新运行。")
        if delay:
            self._wait(delay, countdown=delay >= 5)
        self.last_request = time.monotonic()
        if self.pacer is not None:
            self.pacer.last_request = self.last_request
        if bucket == "search":
            self.search_last = self.last_request

    @staticmethod
    def _retry_after(response) -> Optional[float]:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                return None

    def _request(self, method: str, url: str, **kwargs):
        attempts = 1 if method == "POST" else 4
        for attempt in range(attempts):
            self._pace(url)
            try:
                response = self.session.request(method, url, timeout=(10, self.timeout),
                                                allow_redirects=self.label == 'Zenodo' and method == 'GET',
                                                stream=True, **kwargs)
            except requests.TooManyRedirects as error:
                if error.response is not None:
                    error.response.close()
                if self.label == 'Zenodo' and method == 'GET':
                    raise APIError('Zenodo', 302) from None
                if method == 'POST':
                    raise ModelUnavailable('Ollama 响应重定向异常；停止运行，保留断点。') from None
                raise MinerError(self.label + ' 重定向次数超限；未记录为扫描完成。') from None
            except requests.RequestException:
                if method == "POST":
                    raise ModelUnavailable("Ollama 连接失败或超时；已停止本次运行，保留断点，不自动重试。") from None
                if attempt == attempts - 1:
                    raise MinerError(self.label + " 网络请求失败；未记录为扫描完成。") from None
                self._wait(min(self.max_wait, 2 ** attempt + random.random()))
                continue
            status = response.status_code
            retry_after = self._retry_after(response)
            rate_limited = status == 429 or (status == 403 and (
                response.headers.get("X-RateLimit-Remaining") == "0" or retry_after is not None))
            # GitHub secondary limits can arrive as 403 without rate-limit headers.
            if status == 403 and self.label == "GitHub" and not rate_limited:
                try:
                    body = self._read(response, 32_768).decode("utf-8", errors="replace").lower()
                    rate_limited = "rate limit" in body or "abuse" in body
                except MinerError:
                    rate_limited = False
            if status >= 500 or rate_limited:
                if method == "POST":
                    response.close()
                    raise ModelUnavailable("Ollama 服务暂不可用；已停止本次运行并保留断点。")
                if rate_limited:
                    reset = response.headers.get("X-RateLimit-Reset", "0")
                    try:
                        reset_wait = max(0, float(reset) - time.time())
                    except ValueError:
                        reset_wait = 0
                    # Respect BOTH reset and Retry-After when both are supplied.
                    delay = max(retry_after, reset_wait) if retry_after is not None else (reset_wait or 60)
                    if not reset_wait and retry_after is None:
                        delay *= 2 ** attempt
                else:
                    delay = 2 ** attempt + random.random()
                response.close()
                # Retain the embargo even when this page/item is deferred.
                self.not_before = time.time() + delay
                if attempt == attempts - 1 or (delay > self.max_wait and not (rate_limited and self.continuous_rate_wait)):
                    if self.label == 'Zenodo' and not rate_limited:
                        raise APIError(self.label, status)
                    raise MinerError(self.label + " 频控或临时故障；请稍后重跑，断点已保留。")
                continue
            if status not in {200, 304}:
                if method == 'POST':
                    detail = self._model_error_detail(response)
                    if status in {400, 401, 403, 404, 405, 413, 415, 422}:
                        raise ModelRequestRejected(status, detail)
                    raise ModelUnavailable(self.label + ' HTTP ' + str(status) +
                                           ('；服务端信息：' + detail if detail else '') +
                                           '；请求状态未确认，断点已保留，不自动重发。')
                response.close()
                raise APIError(self.label, status)
            if response.headers.get("X-RateLimit-Remaining") == "0":
                try:
                    reset_time = float(response.headers.get("X-RateLimit-Reset", "0"))
                    bucket = response.headers.get("X-RateLimit-Resource") or ("search" if "/search/" in url else "core")
                    self.bucket_waits[bucket] = reset_time if reset_time > time.time() else time.time() + 60
                except ValueError:
                    self.not_before = time.time() + 60
            return response
        raise MinerError(self.label + " 请求未完成。")

    def _model_error_detail(self, response):
        """Read only a bounded error field, never headers, credentials or an echoed request."""
        try:
            raw = self._read(response, 32_768).decode('utf-8', errors='replace')
            parsed = json.loads(raw)
        except (MinerError, ValueError, UnicodeError, RecursionError):
            response.close()
            return '错误详情不可读或不是 JSON；未输出原始响应体'
        if not isinstance(parsed, dict) or not isinstance(parsed.get('error'), str):
            return '响应未包含字符串 error 字段；未输出原始响应体'
        detail = self.error_sanitizer.clean(parsed['error'])
        # Configured credentials are removed before truncation. Hide URLs/IPs and
        # common credential assignments even if a gateway echoes them in an error.
        detail = re.sub(r'https?://[^\s\"\'<>]+', '[REDACTED_URL]', detail)
        detail = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b', '[REDACTED_IP]', detail)
        detail = re.sub(r'(?i)\bBearer\s+[^\s\"\',;]+', 'Bearer [REDACTED]', detail)
        detail = re.sub(r'(?i)((?:api[_ -]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+',
                        r'\1[REDACTED]', detail)
        return ' '.join(detail.split())[:600]

    def _read(self, response, limit: int) -> bytes:
        parts = []
        size = 0
        deadline = time.monotonic() + self.timeout
        try:
            for chunk in response.iter_content(chunk_size=16_384):
                if self.label != 'Ollama' and self.shutdown and self.shutdown.requested and not getattr(self.shutdown, 'active', 0):
                    from .shutdown import StopRequested
                    raise StopRequested('采集响应已中止，游标未前移。')
                if time.monotonic() > deadline:
                    if self.label == 'Ollama':
                        raise ModelUnavailable('Ollama 响应读取超时；请求状态不明，禁止自动重发。')
                    raise MinerError(self.label + ' 响应读取超过本次时间预算。')
                size += len(chunk)
                if size > limit:
                    if self.label == 'Ollama':
                        raise ModelUnavailable('Ollama 响应超过大小限制；请求状态不明，禁止自动重发。')
                    raise MinerError(self.label + " 响应超过安全大小限制。")
                parts.append(chunk)
            return b"".join(parts)
        except requests.RequestException:
            if self.label == "Ollama":
                raise ModelUnavailable("Ollama 响应中断；已停止本次运行，保留断点。") from None
            raise MinerError(self.label + " 响应中断；未记录为扫描完成。") from None
        finally:
            response.close()

    def get_json(self, path: str, params: Optional[dict] = None, cache: bool = True):
        url = self._url(path)
        request_identity = url + ("?" + urlencode(sorted(params.items())) if params else "")
        key = hashlib.sha256(request_identity.encode()).hexdigest()
        cached = self.state.cache_get(key) if cache and self.state else None
        headers = {}
        if cached:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            elif cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]
        response = self._request("GET", url, params=params, headers=headers)
        if response.status_code == 304:
            response.close()
            if cached is None:
                raise MinerError(self.label + " 返回 304 但本地缓存不存在。")
            return json.loads(cached["payload"])
        etag = response.headers.get("ETag")
        modified = response.headers.get("Last-Modified")
        try:
            payload = json.loads(self._read(response, 20_000_000))
        except (ValueError, UnicodeError, RecursionError):
            raise MinerError(self.label + " 返回无效 JSON。") from None
        if cache and self.state:
            self.state.cache_put(key, etag, modified, payload)
        return payload

    def get_bytes(self, path: str, max_bytes: int = MAX_TEXT_BYTES, params=None) -> bytes:
        response = self._request("GET", self._url(path), params=params)
        return self._read(response, max_bytes)

    def get_text(self, path: str, max_bytes: int = MAX_TEXT_BYTES, params=None) -> str:
        return self.get_bytes(path, max_bytes, params).decode("utf-8", errors="replace")

    def post_json(self, path: str, payload: dict) -> dict:
        response = self._request("POST", self._url(path), json=payload)
        try:
            body = json.loads(self._read(response, 131_072))
        except (ValueError, UnicodeError, RecursionError):
            raise ModelUnavailable("Ollama 返回无效 HTTP JSON；已停止本次运行。") from None
        if not isinstance(body, dict):
            raise ModelUnavailable("Ollama 返回格式异常；已停止本次运行。")
        return body
