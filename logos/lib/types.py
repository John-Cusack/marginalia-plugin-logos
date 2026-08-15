"""Pydantic models for Logos cookie management."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel

# Faithlife SSO has rotated cookie names over time. Check for the current names
# first — `auth2` (app.logos.com) and `auth-services` (auth.faithlife.com) — but
# accept the legacy `auth` so saved jars from older logins keep working.
_AUTH_COOKIE_NAMES: tuple[str, ...] = ("auth2", "auth", "auth-services")


def _domain_matches(cookie_domain: str, target_host: str) -> bool:
    """Check if a cookie domain matches the target host (RFC 6265 rules)."""
    cd = cookie_domain.lower().strip()
    th = target_host.lower().strip()

    if cd == th:
        return True

    # Leading-dot domain: matches host and any subdomain
    if cd.startswith("."):
        bare = cd[1:]
        return th == bare or th.endswith(f".{bare}")

    return False


class LogosCookie(BaseModel):
    name: str
    value: str
    domain: str
    path: str
    expires: float


class LogosCookieJar(BaseModel):
    """All cookies from a login session."""

    cookies: list[LogosCookie]

    def header_value(self, url: str = "https://app.logos.com") -> str:
        """Build a Cookie header string, filtered to cookies matching the URL's host."""
        host = urlparse(url).hostname or "app.logos.com"
        matched = [c for c in self.cookies if _domain_matches(c.domain, host)]
        return "; ".join(f"{c.name}={c.value}" for c in matched)

    @property
    def auth_cookie(self) -> LogosCookie | None:
        # First non-empty cookie matching one of the known auth names.
        for name in _AUTH_COOKIE_NAMES:
            for c in self.cookies:
                if c.name == name and c.value:
                    return c
        return None

    @property
    def auth_cookies(self) -> list[LogosCookie]:
        """All session-bearing cookies present in the jar."""
        return [c for c in self.cookies if c.name in _AUTH_COOKIE_NAMES and c.value]

    def min_auth_expiry(self) -> float | None:
        """Earliest real expiry among auth cookies, or None if all are session cookies.

        Used by the session keeper to decide when a proactive silent renewal is
        due, before the session actually lapses.
        """
        expiries = [c.expires for c in self.auth_cookies if c.expires and c.expires > 0]
        return min(expiries) if expiries else None
