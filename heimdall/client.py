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
        self._http = httpx.Client(base_url=self.url, headers=headers, timeout=timeout,
                                  transport=transport)

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

    def post(self, path: str, body: Any = None, **params) -> Any:
        return self._call("POST", path, json=body,
                          params={k: v for k, v in params.items() if v is not None})

    def put(self, path: str, body: Any) -> Any:
        return self._call("PUT", path, json=body)

    def delete(self, path: str, **params) -> Any:
        return self._call("DELETE", path, params={k: v for k, v in params.items() if v is not None})
