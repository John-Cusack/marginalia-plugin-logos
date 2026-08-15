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
    ensure_session_keeper,
    get_cookie_header,
    refresh_auth,
)
from logos.auth.playwright_login import profile_seeded
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
        # Kick off the background session keeper on first authenticated call.
        ensure_session_keeper()

        cookie = get_cookie_header()
        if not cookie:
            # No stored session. Only worth a silent renewal if the SSO profile
            # has actually been seeded by a prior login — otherwise spinning up a
            # headless browser just to fail ~30s later looks like a hang. When
            # nothing is seeded, fail fast with actionable guidance.
            if not profile_seeded():
                raise RuntimeError(
                    "Not authenticated. Run 'logos-login' to sign in once "
                    "(seeds the SSO profile for silent renewals)."
                )
            try:
                new_jar = await refresh_auth(interactive=False)
                cookie = new_jar.header_value(f"{self.base_url}{path}")
            except Exception as auth_err:
                raise RuntimeError(
                    "Not authenticated. Run 'logos-login' to sign in once "
                    f"(seeds the SSO profile for silent renewals). ({auth_err})"
                ) from auth_err
            if not cookie:
                # Renewal succeeded but produced no cookie matching this host.
                raise RuntimeError(
                    "Not authenticated: session renewal produced no usable cookie. "
                    "Run 'logos-login'."
                )

        url = f"{self.base_url}{path}"
        has_body = body is not None

        log_debug(f"{method} {url}")

        headers = self._headers(cookie, has_body=has_body)
        kwargs: dict[str, Any] = {"headers": headers}
        if has_body:
            kwargs["json"] = body

        client = await self._get_client()
        response = await client.request(method, url, **kwargs)

        # 401 → re-auth → retry once. refresh_auth is non-destructive (obtains a
        # fresh session before replacing the stored one), so we do NOT clear
        # cookies first: a failed renewal must not wipe a still-valid session
        # over a transient 401. interactive=False — never open a browser from a
        # running server; the keeper / CLI handle re-seeding the SSO profile.
        if response.status_code == 401:
            log("Got 401, attempting re-authentication...")
            try:
                new_jar = await refresh_auth(interactive=False)
                new_cookie = new_jar.header_value(url)
                if not new_cookie:
                    # Renewal reported success but captured no cookie for this
                    # host (e.g. only an auth.faithlife.com cookie). Retrying
                    # would just send an unauthenticated request — surface the
                    # actionable error instead.
                    raise RuntimeError(
                        "re-authentication produced no usable cookie for this host"
                    )
                headers = self._headers(new_cookie, has_body=has_body)
                kwargs["headers"] = headers
                response = await client.request(method, url, **kwargs)
            except Exception as auth_err:
                raise RuntimeError(
                    f"Authentication failed. Run 'logos-login' manually. ({auth_err})"
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
