"""httpx AsyncClient with cookie injection and 401 retry.

A single long-lived AsyncClient is held per LogosClient instance. This keeps
connections warm between requests so each fetch reuses an established TLS
session instead of paying a fresh handshake — typically saves 50–150ms per
request on a sustained walk. See PIPELINED_INGEST.md and the timing data
that motivated this change.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from logos.auth.manager import (
    get_cookie_header,
    refresh_auth,
)
from logos.lib.constants import BASE_URL
from logos.lib.logger import log, log_debug


class LogosClient:
    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url
        # Lazily initialized in _get_client so the AsyncClient is bound to
        # the running event loop, not whichever loop happened to exist at
        # module import time.
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=120)
        return self._client

    async def close(self) -> None:
        """Close the underlying AsyncClient. Safe to call multiple times."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, cookie_header: str | None, *, has_body: bool = False) -> dict[str, str]:
        headers = {
            "Origin": "https://app.logos.com",
            "Referer": "https://app.logos.com/",
            "Accept": "application/json",
            "X-Requested-With": "fetch",
        }
        if not has_body:
            headers["Content-Type"] = "application/json"
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        stream: bool = False,
    ) -> Any:
        cookie = get_cookie_header()
        if not cookie:
            raise RuntimeError("Not authenticated. Run the Logos auth flow to log in.")

        url = f"{self.base_url}{path}"
        has_body = body is not None

        log_debug(f"{method} {url}")

        headers = self._headers(cookie, has_body=has_body)
        kwargs: dict[str, Any] = {"headers": headers}
        if has_body:
            kwargs["json"] = body

        client = await self._get_client()
        response = await client.request(method, url, **kwargs)

        # 401 → re-auth → retry once.
        # refresh_auth() handles the cache + file clear internally; no need
        # to call invalidate_auth/logout beforehand.
        if response.status_code == 401:
            log("Got 401, attempting re-authentication...")
            try:
                new_jar = await refresh_auth()
                new_cookie = new_jar.header_value(url)
                headers = self._headers(new_cookie, has_body=has_body)
                kwargs["headers"] = headers
                response = await client.request(method, url, **kwargs)
            except Exception as auth_err:
                raise RuntimeError(
                    f"Authentication failed. Run the Logos auth flow manually. ({auth_err})"
                ) from auth_err

        response.raise_for_status()

        if stream:
            return response

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    async def get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self.request("POST", path, body=body)

    async def get_stream(self, path: str) -> httpx.Response:
        return await self.request("GET", path, stream=True)

    async def post_stream(self, path: str, body: Any = None) -> httpx.Response:
        return await self.request("POST", path, body=body, stream=True)


# Module-level singleton
logos_client = LogosClient()
