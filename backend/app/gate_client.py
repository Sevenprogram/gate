"""Signed HTTP client for the Gate APIv4 private endpoints.

The signature scheme (APIv4) is:

    payload_hash = sha512(body).hexdigest()          # sha512("") when there is no body
    sign_string  = "\\n".join([METHOD, path, query_string, payload_hash, timestamp])
    SIGN         = hmac_sha512(secret, sign_string).hexdigest()

`path` must include the /api/v4 prefix and `query_string` must be the raw
query exactly as it goes on the wire, with no leading "?".
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from .config import Profile

log = logging.getLogger(__name__)

API_PREFIX = "/api/v4"
EMPTY_BODY_HASH = hashlib.sha512(b"").hexdigest()

Method = Literal["GET", "POST", "DELETE", "PUT"]


class GateAPIError(RuntimeError):
    """A non-2xx response from Gate, with the label/message it returned."""

    def __init__(self, status: int, label: str, message: str, path: str) -> None:
        self.status = status
        self.label = label
        self.message = message
        self.path = path
        super().__init__(f"{status} {label} on {path}: {message}")


class MissingCredentials(RuntimeError):
    pass


class _RateLimiter:
    """Spaces out requests so a manual refresh burst can't trip Gate's limiter.

    Gate buckets private endpoints per key (order of 10 req/s). A dashboard
    refresh fans out to a couple dozen calls, so we cap concurrency and keep a
    floor on the gap between request starts.
    """

    def __init__(self, max_concurrency: int = 4, min_interval: float = 0.12) -> None:
        self._sem = asyncio.Semaphore(max_concurrency)
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    async def __aenter__(self) -> None:
        await self._sem.acquire()
        async with self._lock:
            gap = time.monotonic() - self._last_start
            if gap < self._min_interval:
                await asyncio.sleep(self._min_interval - gap)
            self._last_start = time.monotonic()

    async def __aexit__(self, *exc_info: object) -> None:
        self._sem.release()


class GateClient:
    """One signed client per profile.

    Gate meters its rate limits per API key, so each profile gets its own
    limiter and connection pool rather than sharing one — otherwise a busy
    account would throttle a quiet one for no reason.
    """

    def __init__(self, profile: Profile, api_host: str) -> None:
        self._profile = profile
        self._limiter = _RateLimiter()
        self._http = httpx.AsyncClient(
            base_url=api_host,
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    @property
    def profile(self) -> Profile:
        return self._profile

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ signing

    def _sign(self, method: Method, path: str, query: str, body: str) -> dict[str, str]:
        if not self._profile.has_credentials:
            raise MissingCredentials(
                f"profile '{self._profile.id}' has no credentials — add a read-only "
                "key to profiles.toml, or to .env for a single-account setup"
            )
        timestamp = str(int(time.time()))
        payload_hash = EMPTY_BODY_HASH if not body else hashlib.sha512(body.encode()).hexdigest()
        sign_string = "\n".join([method, path, query, payload_hash, timestamp])
        signature = hmac.new(
            self._profile.api_secret.encode(),
            sign_string.encode(),
            hashlib.sha512,
        ).hexdigest()
        return {"KEY": self._profile.api_key, "Timestamp": timestamp, "SIGN": signature}

    # ------------------------------------------------------------------ requests

    async def request(
        self,
        method: Method,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
        signed: bool = True,
    ) -> Any:
        path = f"{API_PREFIX}{endpoint}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        # doseq keeps list params (e.g. status filters) on the wire the same way
        # they get folded into the signature.
        query = urlencode(clean_params, doseq=True)
        body_text = "" if body is None else json.dumps(body)

        headers = self._sign(method, path, query, body_text) if signed else {}
        url = f"{path}?{query}" if query else path

        async with self._limiter:
            response = await self._http.request(
                method,
                url,
                headers=headers,
                content=body_text or None,
            )

        if response.status_code >= 400:
            label, message = _extract_error(response)
            raise GateAPIError(response.status_code, label, message, path)

        if not response.content:
            return None
        return response.json()

    async def get(self, endpoint: str, **kwargs: Any) -> Any:
        return await self.request("GET", endpoint, **kwargs)

    async def public_get(self, endpoint: str, **kwargs: Any) -> Any:
        return await self.request("GET", endpoint, signed=False, **kwargs)

    # ---------------------------------------------------------------- pagination

    async def paginate_by_time(
        self,
        endpoint: str,
        *,
        start: int,
        end: int,
        params: dict[str, Any] | None = None,
        limit: int = 1000,
        time_field: str = "time",
        max_pages: int = 60,
    ) -> list[dict[str, Any]]:
        """Walk a time-ranged history endpoint backwards from `end` to `start`.

        Gate only offers offset/limit paging on these endpoints and deep offsets
        get slow, so we shrink the `to` bound instead: each page asks for the
        newest `limit` records at or before the oldest record we've already seen.
        Both bounds are unix seconds.
        """
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor = end

        for page in range(max_pages):
            batch = await self.get(
                endpoint,
                params={**(params or {}), "from": start, "to": cursor, "limit": limit},
            )
            if not batch:
                break

            fresh = [row for row in batch if _row_key(row) not in seen]
            for row in fresh:
                seen.add(_row_key(row))
            collected.extend(fresh)

            if len(batch) < limit:
                break

            oldest = min(int(float(row.get(time_field, cursor))) for row in batch)
            if oldest <= start:
                break
            if not fresh:
                # Every record on this page was already known and the bound
                # isn't moving — a single second holds more than `limit` rows.
                # Step back one second so we make progress instead of spinning.
                cursor = oldest - 1
            else:
                cursor = oldest
            if page == max_pages - 1:
                log.warning(
                    "paginate_by_time hit max_pages=%d on %s; history may be truncated",
                    max_pages,
                    endpoint,
                )

        return collected


    async def paginate_by_offset(
        self,
        endpoint: str,
        *,
        since: int,
        params: dict[str, Any] | None = None,
        limit: int = 1000,
        time_field: str = "create_time",
        max_pages: int = 30,
    ) -> list[dict[str, Any]]:
        """Page through an endpoint that only supports offset/limit.

        Stops as soon as a page contains a record older than `since`, so the
        offset never grows past the window the caller asked for. Used for the
        trade endpoints, which do not all accept from/to bounds.
        """
        collected: list[dict[str, Any]] = []
        for page in range(max_pages):
            batch = await self.get(
                endpoint,
                params={**(params or {}), "limit": limit, "offset": page * limit},
            )
            if not batch:
                break
            collected.extend(batch)
            oldest = min(int(float(row.get(time_field, since))) for row in batch)
            if oldest <= since or len(batch) < limit:
                break
            if page == max_pages - 1:
                log.warning(
                    "paginate_by_offset hit max_pages=%d on %s; history may be truncated",
                    max_pages,
                    endpoint,
                )
        return [row for row in collected if int(float(row.get(time_field, since))) >= since]


def _row_key(row: dict[str, Any]) -> str:
    """Best-effort stable identity for a history row, for dedup across pages."""
    for field in ("id", "trade_id", "order_id"):
        value = row.get(field)
        if value not in (None, ""):
            return f"{field}:{value}"
    return json.dumps(row, sort_keys=True)


class ClientRegistry:
    """Holds one GateClient per configured profile.

    Profiles come and go at runtime through the account-management UI, so the
    registry grows and shrinks alongside the profile store.
    """

    def __init__(self, profiles: tuple[Profile, ...], api_host: str) -> None:
        self._api_host = api_host
        self._clients = {p.id: GateClient(p, api_host) for p in profiles}

    def get(self, profile_id: str) -> GateClient:
        client = self._clients.get(profile_id)
        if client is None:
            raise KeyError(profile_id)
        return client

    def add(self, profile: Profile) -> None:
        if profile.id in self._clients:
            raise ValueError(f"profile '{profile.id}' already has a client")
        self._clients[profile.id] = GateClient(profile, self._api_host)

    async def remove(self, profile_id: str) -> None:
        client = self._clients.pop(profile_id, None)
        if client is not None:
            await client.aclose()

    def __iter__(self):
        return iter(self._clients.items())

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()


def _extract_error(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return ("HTTP_ERROR", response.text[:400])
    if isinstance(payload, dict):
        return (
            str(payload.get("label") or "HTTP_ERROR"),
            str(payload.get("message") or payload.get("detail") or payload),
        )
    return ("HTTP_ERROR", str(payload)[:400])
