"""A small HTTP client for Huginn's API, shared by Bifrost and Sleipnir."""
from __future__ import annotations

from typing import Any, Optional

import httpx


class HuginnError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        self.status = status
        self.detail = detail
        if isinstance(detail, dict) and "problems" in detail:
            text = "; ".join(detail["problems"])
        else:
            text = str(detail)
        super().__init__(text)

    @property
    def problems(self) -> list[str]:
        if isinstance(self.detail, dict) and "problems" in self.detail:
            return list(self.detail["problems"])
        return [str(self.detail)]


class HuginnUnreachable(RuntimeError):
    pass


class HuginnClient:
    def __init__(self, url: str, token: str = "", user: str = "", timeout: float = 120.0,
                 transport: Optional[httpx.BaseTransport] = None):
        headers = {}
        if token:
            headers["X-Heimdall-Token"] = token
        if user:
            headers["X-Heimdall-User"] = user
        self.url = url.rstrip("/")
        # trust_env=False: Huginn is on this machine or the office network, never behind
        # a proxy. Otherwise httpx takes the system proxy (on Windows from the registry,
        # ignoring its "bypass for local addresses" list) and sends even 127.0.0.1
        # through it, where a company proxy blocks it.
        self._http = httpx.Client(base_url=self.url, headers=headers, timeout=timeout,
                                  transport=transport, trust_env=False)

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw) -> Any:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise HuginnUnreachable(f"cannot reach Huginn at {self.url}: {exc}") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise HuginnError(r.status_code, detail)
        return r.json() if r.content else None

    def get(self, path: str, **params) -> Any:
        return self._call("GET", path, params={k: v for k, v in params.items() if v is not None})

    def get_bytes(self, path: str, **params) -> bytes:
        """A file rather than JSON (a sound to preview)."""
        try:
            r = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.TransportError as exc:
            raise HuginnUnreachable(f"cannot reach Huginn at {self.url}: {exc}") from None
        if r.status_code >= 400:
            raise HuginnError(r.status_code, r.text)
        return r.content

    def post(self, path: str, body: Any = None, **params) -> Any:
        return self._call("POST", path, json=body,
                          params={k: v for k, v in params.items() if v is not None})

    def put(self, path: str, body: Any) -> Any:
        return self._call("PUT", path, json=body)

    def delete(self, path: str, **params) -> Any:
        return self._call("DELETE", path, params={k: v for k, v in params.items() if v is not None})
