"""MiniMax bridge used to perform the actual code modification.

The assistant that performs the change is a MiniMax model.  This module talks
to the Anthropic-compatible Messages endpoint that MiniMax exposes, sends the
development context assembled from seekdb and PowerContext, and returns the
modified file together with the raw exchange so the run can be audited.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.minimax.cn/v1"
DEFAULT_MODEL = "MiniMax-M3"


class MiniMaxError(RuntimeError):
    """Raised when the MiniMax endpoint cannot be reached or answers badly."""


@dataclass
class MiniMaxExchange:
    """One recorded request/response pair."""

    model: str
    base_url: str
    prompt_chars: int
    response_chars: int
    elapsed_ms: int
    response_text: str
    raw_response: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "prompt_chars": self.prompt_chars,
            "response_chars": self.response_chars,
            "elapsed_ms": self.elapsed_ms,
            "usage": self.raw_response.get("usage", {}),
        }


def _read_api_key(explicit: str | None) -> str:
    """Resolve the credential used for the MiniMax endpoint."""

    if explicit:
        return explicit.strip()
    env_key = os.environ.get("MINIMAX_API_KEY")
    if env_key:
        return env_key.strip()
    config = Path(os.environ.get("MINIMAX_CONFIG", Path.home() / ".minimax" / "config.yaml"))
    if config.exists():
        for line in config.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("apiKey:"):
                value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                # The shipped profile carries a placeholder key; only a real
                # looking key is used here.
                if len(value) >= 20:
                    return value
    token = _read_oauth_token()
    if token:
        return token
    raise MiniMaxError(
        "no MiniMax credential found: set MINIMAX_API_KEY, add a key to "
        "~/.minimax/config.yaml, or sign in to MiniMax Code"
    )


def _read_oauth_token() -> str | None:
    """Read the signed-in MiniMax Code access token when it exists."""

    base = Path(os.environ.get("MINIMAX_CONFIG", Path.home() / ".minimax" / "config.yaml")).parent
    auth_root = base / "auth"
    if not auth_root.is_dir():
        return None
    for path in sorted(auth_root.rglob("auth.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for record in data.get("records", {}).values():
            token = record.get("accessToken")
            if token:
                return token
    return None


def _read_base_url(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    env_url = os.environ.get("MINIMAX_BASE_URL")
    if env_url:
        return env_url.rstrip("/")
    config = Path(os.environ.get("MINIMAX_CONFIG", Path.home() / ".minimax" / "config.yaml"))
    if config.exists():
        for line in config.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("baseURL:"):
                value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value.rstrip("/")
    if os.environ.get("MINIMAX_API_KEY"):
        # An explicit public API key is used against the public endpoint.
        return DEFAULT_BASE_URL
    return DEFAULT_BASE_URL


class MiniMaxClient:
    """Minimal Anthropic-compatible MiniMax client built on the standard library."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: int = 300,
    ) -> None:
        self.api_key = _read_api_key(api_key)
        self.base_url = _read_base_url(base_url)
        self.model = model
        self.timeout = timeout
        self.exchanges: list[MiniMaxExchange] = []

    def complete(self, prompt: str, *, max_tokens: int = 16000) -> MiniMaxExchange:
        """Send one prompt and return the model's answer."""

        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/messages",
            data=body,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network failure path
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise MiniMaxError(f"MiniMax HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network failure path
            raise MiniMaxError(f"MiniMax request failed: {exc.reason}") from exc
        elapsed = int((time.time() - started) * 1000)

        parsed = json.loads(raw)
        text = _extract_text(parsed)
        exchange = MiniMaxExchange(
            model=self.model,
            base_url=self.base_url,
            prompt_chars=len(prompt),
            response_chars=len(text),
            elapsed_ms=elapsed,
            response_text=text,
            raw_response=parsed,
        )
        self.exchanges.append(exchange)
        return exchange


def _extract_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        return "\n".join(part for part in parts if part)
    if isinstance(content, str):
        return content
    if isinstance(payload.get("choices"), list):
        choices = payload["choices"]
        message = choices[0].get("message", {}) if choices else {}
        return message.get("content", "")
    return ""
