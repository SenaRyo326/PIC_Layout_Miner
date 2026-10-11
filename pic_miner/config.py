"""Configuration and output sanitization; endpoint values never enter logs."""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple
from urllib.parse import urlsplit

from dotenv import load_dotenv


MAX_EVIDENCE_CHARS = 10_000
NUM_CTX = 8192
MAX_MODEL_EVIDENCE_CHARS = 3500
RETRY_MODEL_EVIDENCE_CHARS = 1500
MAX_GENERATION_TOKENS = 2048
RETRY_GENERATION_TOKENS = 4096
MAX_TEXT_BYTES = 131_072
MAX_SOURCE_BYTES = 8 * 1024 * 1024
PIPELINE_VERSION = "pic-miner-v8-pic-asset-precision"


class MinerError(Exception):
    """An error whose message is safe to display without a traceback."""


class ModelUnavailable(MinerError):
    """Stop the entire run: a disconnected model request may still be running."""


class ModelRequestRejected(ModelUnavailable):
    """The server returned a definitive client error; fix the request before restarting."""
    def __init__(self, status, detail=''):
        self.status = status
        message = 'Ollama HTTP ' + str(status) + '；服务端拒绝请求'
        if detail:
            message += '：' + detail
        super().__init__(message + '；请检查请求参数、模型名称或 API 地址，断点已保留。')


class ModelOutputError(MinerError):
    """Completed generation could not be validated; retain it for human review."""


class APIError(MinerError):
    def __init__(self, label: str, status: int):
        self.status = status
        super().__init__(label + " HTTP " + str(status) + "；未记录为扫描完成。")


class Sanitizer:
    def __init__(self, secrets: Tuple[str, ...]):
        self.secrets = sorted({value for value in secrets if value}, key=len, reverse=True)

    def clean(self, value: object) -> str:
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


@dataclass(frozen=True)
class Config:
    root: Path
    ollama_base_url: str = field(repr=False)
    ollama_model: str = field(repr=False)
    github_token: str = field(repr=False)
    github_api_url: str = field(default="https://api.github.com", repr=False)
    zenodo_api_url: str = field(default="https://zenodo.org/api", repr=False)
    http_timeout: float = 30
    ollama_timeout: float = 300
    max_retry_wait: float = 30
    arxiv_api_url: str = field(default="https://export.arxiv.org/api", repr=False)
    arxiv_content_url: str = field(default="https://arxiv.org", repr=False)
    discovery_interval: float = 2.5
    repo_recheck_interval: float = 1800
    idle_interval: float = .25
    discovery_overlap: float = 600
    prefetch_per_source: int = 8
    model_chunks: int = 8
    model_keep_alive: str = '-1'

    @classmethod
    def load(cls, root: Path) -> "Config":
        env_path = root / ".env"
        if env_path.is_file():
            load_dotenv(env_path, override=False)
        names = ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "GITHUB_TOKEN")
        missing = [name for name in names if not os.getenv(name, "").strip()]
        if missing:
            raise MinerError("缺少配置：" + ", ".join(missing) + "。请保存并填写工程根目录的 .env。")
        endpoints = {
            "OLLAMA_BASE_URL": os.environ["OLLAMA_BASE_URL"].strip().rstrip("/"),
            "GITHUB_API_URL": os.getenv("GITHUB_API_URL", "https://api.github.com").strip().rstrip("/"),
            "ZENODO_API_URL": os.getenv("ZENODO_API_URL", "https://zenodo.org/api").strip().rstrip("/"),
        }
        endpoints["ARXIV_API_URL"] = os.getenv("ARXIV_API_URL", "https://export.arxiv.org/api").strip().rstrip("/")
        endpoints["ARXIV_CONTENT_URL"] = os.getenv("ARXIV_CONTENT_URL", "https://arxiv.org").strip().rstrip("/")
        for name, value in endpoints.items():
            try:
                parts = urlsplit(value)
                valid = (parts.scheme in {"http", "https"} and parts.hostname
                         and not parts.username and not parts.password
                         and not parts.query and not parts.fragment)
                parts.port
            except ValueError:
                valid = False
            if not valid:
                raise MinerError(name + " 必须是无凭证、无查询参数的 HTTP(S) 基础地址。")
        if urlsplit(endpoints["GITHUB_API_URL"]).scheme != "https":
            raise MinerError("GITHUB_API_URL 必须使用 HTTPS，防止 Token 明文传输。")
        try:
            timeout = float(os.getenv("HTTP_TIMEOUT", "30"))
            model_timeout = float(os.getenv("OLLAMA_TIMEOUT", "300"))
            max_wait = float(os.getenv("MAX_RETRY_WAIT", "30"))
            if not all(0 < number <= 3600 for number in (timeout, model_timeout, max_wait)):
                raise ValueError("range")
        except ValueError:
            raise MinerError("超时配置必须为 0 到 3600 之间的有限正数。") from None
        schedule = {}
        for name, default, lower, upper in (
                ("DISCOVERY_INTERVAL", 2.5, 2.5, 604800),
                ("REPO_RECHECK_INTERVAL", 1800, 2.5, 604800),
                ("IDLE_INTERVAL", .25, .05, 60),
                ("DISCOVERY_OVERLAP", 600, 60, 86400)):
            try:
                number = float(os.getenv(name, str(default)))
                if not lower <= number <= upper:
                    raise ValueError("range")
                schedule[name.lower()] = number
            except ValueError:
                raise MinerError(name + " 必须是范围内的有限正数。") from None
        for name, default, upper in (("PREFETCH_PER_SOURCE", 8, 100), ("MODEL_CHUNKS", 8, 256)):
            try:
                number = int(os.getenv(name, str(default)))
                if not 1 <= number <= upper:
                    raise ValueError('range')
                schedule[name.lower()] = number
            except ValueError:
                raise MinerError(name + " 必须为范围内的正整数。") from None
        keep_alive = os.getenv('MODEL_KEEP_ALIVE', '-1').strip()
        if not re.fullmatch(r'-1|\d+|\d+(?:s|m|h)', keep_alive):
            raise MinerError('MODEL_KEEP_ALIVE 必须为 -1、非负秒数或带 s/m/h 的时长。')
        schedule['model_keep_alive'] = keep_alive
        return cls(root, endpoints["OLLAMA_BASE_URL"], os.environ["OLLAMA_MODEL"].strip(),
                   os.environ["GITHUB_TOKEN"].strip(), endpoints["GITHUB_API_URL"],
                   endpoints["ZENODO_API_URL"], timeout, model_timeout, max_wait,
                   arxiv_api_url=endpoints["ARXIV_API_URL"], arxiv_content_url=endpoints["ARXIV_CONTENT_URL"], **schedule)

    @property
    def sanitizer(self) -> Sanitizer:
        parts = urlsplit(self.ollama_base_url)
        secrets = [self.github_token, self.ollama_base_url, parts.netloc]
        if parts.hostname and len(parts.hostname) >= 4:
            secrets.append(parts.hostname)
        return Sanitizer(tuple(secrets))

    @property
    def pipeline_key(self) -> str:
        from .judge import Assessment, SYSTEM_PROMPT, PAPER_SYSTEM_PROMPT
        settings = {"version": PIPELINE_VERSION, "model": self.ollama_model,
                    "schema": Assessment.model_json_schema(),
                    "prompts": [SYSTEM_PROMPT, PAPER_SYSTEM_PROMPT], "model_chunks": self.model_chunks,
                    "num_ctx": NUM_CTX, "assessment": "three-level-enum",
                    "evidence_chars": MAX_EVIDENCE_CHARS, "text_bytes": MAX_TEXT_BYTES,
                    "model_evidence_chars": MAX_MODEL_EVIDENCE_CHARS,
                    "retry_evidence_chars": RETRY_MODEL_EVIDENCE_CHARS, "num_predict": MAX_GENERATION_TOKENS,
                    "retry_num_predict": RETRY_GENERATION_TOKENS, "think": False,
                    "source_bytes": MAX_SOURCE_BYTES}
        return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
