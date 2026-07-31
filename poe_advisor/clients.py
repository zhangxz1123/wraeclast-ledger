from __future__ import annotations

import gzip
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .models import DataSourceError, FetchResult, iso_utc


Transport = Callable[[Request, float], Any]


@dataclass(slots=True)
class ClientConfig:
    timeout_seconds: float = 20.0
    max_retries: int = 2
    max_retry_after_seconds: float = 30.0
    user_agent: str = ""

    @classmethod
    def from_environment(cls) -> "ClientConfig":
        contact = os.environ.get("POE_ADVISOR_CONTACT", "local-personal-use")
        return cls(
            timeout_seconds=float(os.environ.get("POE_ADVISOR_TIMEOUT", "20")),
            max_retries=int(os.environ.get("POE_ADVISOR_RETRIES", "2")),
            max_retry_after_seconds=float(
                os.environ.get("POE_ADVISOR_MAX_RETRY_AFTER", "30")
            ),
            user_agent=(
                f"OAuth poe-market-advisor/0.1.0 (contact: {contact}) "
                "local-price-history"
            ),
        )


class HttpJsonClient:
    """Small conditional-GET client with conservative retry handling."""

    def __init__(
        self,
        config: ClientConfig | None = None,
        *,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.config = config or ClientConfig.from_environment()
        self.transport = transport or self._default_transport
        self.sleeper = sleeper

    @staticmethod
    def _default_transport(request: Request, timeout: float) -> Any:
        return urlopen(request, timeout=timeout)

    def get_json(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        bearer_token: str | None = None,
        extra_headers: dict[str, str] | None = None,
        accept_error_json_statuses: set[int] | None = None,
    ) -> FetchResult:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": self.config.user_agent,
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        if extra_headers:
            headers.update(extra_headers)

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            request = Request(url, headers=headers, method="GET")
            try:
                response = self.transport(request, self.config.timeout_seconds)
                try:
                    status = int(getattr(response, "status", response.getcode()))
                    raw = response.read()
                    response_headers = {
                        str(key).lower(): str(value)
                        for key, value in response.headers.items()
                    }
                finally:
                    close = getattr(response, "close", None)
                    if close:
                        close()
                if response_headers.get("content-encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                payload = self._decode_json(raw, url)
                return FetchResult(
                    url=url,
                    status=status,
                    payload=payload,
                    raw=raw,
                    etag=response_headers.get("etag"),
                    last_modified=response_headers.get("last-modified"),
                    fetched_at=iso_utc(),
                    headers=response_headers,
                )
            except HTTPError as error:
                if error.code == 304:
                    return FetchResult(
                        url=url,
                        status=304,
                        payload=None,
                        raw=b"",
                        etag=error.headers.get("ETag") or etag,
                        last_modified=error.headers.get("Last-Modified")
                        or last_modified,
                        fetched_at=iso_utc(),
                        headers={
                            str(key).lower(): str(value)
                            for key, value in error.headers.items()
                        },
                        not_modified=True,
                    )
                if accept_error_json_statuses and error.code in accept_error_json_statuses:
                    raw = error.read()
                    response_headers = {
                        str(key).lower(): str(value)
                        for key, value in error.headers.items()
                    }
                    if response_headers.get("content-encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    return FetchResult(
                        url=url,
                        status=error.code,
                        payload=self._decode_json(raw, url),
                        raw=raw,
                        etag=response_headers.get("etag"),
                        last_modified=response_headers.get("last-modified"),
                        fetched_at=iso_utc(),
                        headers=response_headers,
                    )
                last_error = error
                retryable = error.code == 429 or 500 <= error.code < 600
                if retryable and attempt < self.config.max_retries:
                    self.sleeper(self._retry_delay(error.headers, attempt))
                    continue
                try:
                    body = error.read(300).decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                raise DataSourceError(
                    f"GET {url} returned HTTP {error.code}"
                    + (f": {body}" if body else "")
                ) from error
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt < self.config.max_retries:
                    self.sleeper(min(2**attempt, self.config.max_retry_after_seconds))
                    continue
                raise DataSourceError(f"GET {url} failed: {error}") from error
        raise DataSourceError(f"GET {url} failed: {last_error}")

    @staticmethod
    def _decode_json(raw: bytes, url: str) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DataSourceError(f"GET {url} did not return valid JSON") from error

    def _retry_delay(self, headers: Any, attempt: int) -> float:
        raw = headers.get("Retry-After") if headers else None
        delay: float | None = None
        if raw:
            try:
                delay = float(raw)
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(str(raw))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = max(
                        0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()
                    )
                except (TypeError, ValueError, OverflowError):
                    delay = None
        if delay is None:
            delay = float(2**attempt)
        return min(delay, self.config.max_retry_after_seconds)


class PoeNinjaClient:
    """Client for poe.ninja's documented economy-only endpoints."""

    SOURCE = "poe.ninja"

    def __init__(
        self,
        http: HttpJsonClient | None = None,
        base_url: str = "https://poe.ninja",
    ):
        self.http = http or HttpJsonClient()
        self.base_url = base_url.rstrip("/")

    def league_url(self) -> str:
        return f"{self.base_url}/poe1/api/economy/leagues"

    def list_leagues(
        self, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        return self.http.get_json(
            self.league_url(), etag=etag, last_modified=last_modified
        )

    def exchange_url(self, league: str, category: str) -> str:
        query = urlencode({"league": league, "type": category})
        return (
            f"{self.base_url}/poe1/api/economy/exchange/current/overview?{query}"
        )

    def fetch_exchange(
        self,
        league: str,
        category: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        return self.http.get_json(
            self.exchange_url(league, category),
            etag=etag,
            last_modified=last_modified,
            extra_headers={"Referer": f"{self.base_url}/economy/"},
        )

    def stash_item_url(self, league: str, category: str) -> str:
        query = urlencode({"league": league, "type": category})
        return (
            f"{self.base_url}/poe1/api/economy/stash/current/item/overview?{query}"
        )

    def fetch_stash_item(
        self,
        league: str,
        category: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        return self.http.get_json(
            self.stash_item_url(league, category),
            etag=etag,
            last_modified=last_modified,
            extra_headers={"Referer": f"{self.base_url}/economy/"},
        )

    def stash_currency_url(self, league: str, category: str) -> str:
        query = urlencode({"league": league, "type": category})
        return (
            f"{self.base_url}/poe1/api/economy/stash/current/currency/overview?"
            f"{query}"
        )

    def fetch_stash_currency(
        self,
        league: str,
        category: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        return self.http.get_json(
            self.stash_currency_url(league, category),
            etag=etag,
            last_modified=last_modified,
            extra_headers={"Referer": f"{self.base_url}/economy/"},
        )

    def exchange_details_url(
        self,
        league: str,
        category: str,
        item_id: int | str,
    ) -> str:
        query = urlencode(
            {"league": league, "type": category, "id": str(item_id)}
        )
        return (
            f"{self.base_url}/poe1/api/economy/exchange/current/details?"
            f"{query}"
        )

    def fetch_exchange_details(
        self,
        league: str,
        category: str,
        item_id: int | str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        return self.http.get_json(
            self.exchange_details_url(league, category, item_id),
            etag=etag,
            last_modified=last_modified,
            extra_headers={"Referer": f"{self.base_url}/economy/"},
        )

    def stash_item_history_url(
        self,
        league: str,
        category: str,
        item_id: int | str,
    ) -> str:
        query = urlencode(
            {"league": league, "type": category, "id": str(item_id)}
        )
        return (
            f"{self.base_url}/poe1/api/economy/stash/current/item/history?"
            f"{query}"
        )

    def fetch_stash_item_history(
        self,
        league: str,
        category: str,
        item_id: int | str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        return self.http.get_json(
            self.stash_item_history_url(league, category, item_id),
            etag=etag,
            last_modified=last_modified,
            extra_headers={"Referer": f"{self.base_url}/economy/"},
        )


class GGGClient:
    """Official Path of Exile API client.

    Currency-exchange history is a public static-data feed. League metadata is
    a separate OAuth service resource and is used only when a token is supplied.
    """

    SOURCE = "ggg-currency-exchange"

    def __init__(
        self,
        oauth_token: str | None = None,
        http: HttpJsonClient | None = None,
        base_url: str = "https://web.poecdn.com/api",
        service_base_url: str = "https://api.pathofexile.com",
    ):
        self.oauth_token = oauth_token or os.environ.get("POE_OAUTH_TOKEN")
        self.http = http or HttpJsonClient()
        self.base_url = base_url.rstrip("/")
        self.service_base_url = service_base_url.rstrip("/")

    @property
    def configured(self) -> bool:
        return True

    @property
    def leagues_configured(self) -> bool:
        return bool(self.oauth_token)

    def leagues_url(self) -> str:
        return f"{self.service_base_url}/league?realm=pc&type=main&limit=50"

    def list_leagues(self) -> FetchResult:
        if not self.oauth_token:
            raise DataSourceError(
                "Official GGG API is not configured; set POE_OAUTH_TOKEN "
                "with the service:leagues scope"
            )
        return self.http.get_json(
            self.leagues_url(), bearer_token=self.oauth_token
        )

    def currency_exchange_url(self, cursor: int | None = None) -> str:
        url = f"{self.base_url}/currency-exchange"
        if cursor is not None:
            url += f"/{quote(str(int(cursor)), safe='')}"
        return url

    def fetch_currency_exchange(self, cursor: int | None = None) -> FetchResult:
        # The CDN currently uses a JSON 404 with an empty markets array to
        # signal that the requested/current hour is not available yet.
        return self.http.get_json(
            self.currency_exchange_url(cursor), accept_error_json_statuses={404}
        )


class GGGSkillTreeClient:
    """Official passive-tree export published by Grinding Gear Games."""

    SOURCE = "ggg-skilltree-export"

    def __init__(
        self,
        http: HttpJsonClient | None = None,
        data_url: str = (
            "https://raw.githubusercontent.com/grindinggear/"
            "skilltree-export/master/data.json"
        ),
    ):
        self.http = http or HttpJsonClient()
        self.data_url = data_url

    def export_url(self) -> str:
        return self.data_url

    def fetch_export(
        self, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        return self.http.get_json(
            self.export_url(),
            etag=etag,
            last_modified=last_modified,
        )


class PoeWatchClient:
    """Community market source used for metadata and historical prices."""

    SOURCE = "poe.watch"

    def __init__(
        self,
        http: HttpJsonClient | None = None,
        base_url: str = "https://api.poe.watch",
    ):
        self.http = http or HttpJsonClient()
        self.base_url = base_url.rstrip("/")

    def leagues_url(self) -> str:
        return f"{self.base_url}/leagues"

    def list_leagues(
        self, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        return self.http.get_json(
            self.leagues_url(), etag=etag, last_modified=last_modified
        )

    def compact_url(self, league: str, *, all_items: bool = True) -> str:
        query = urlencode(
            {
                "league": league,
                "all": "true" if all_items else "false",
            }
        )
        return f"{self.base_url}/compact?{query}"

    def fetch_compact(
        self,
        league: str,
        *,
        all_items: bool = True,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        return self.http.get_json(
            self.compact_url(league, all_items=all_items),
            etag=etag,
            last_modified=last_modified,
        )

    def history_url(self, league: str, item_id: int | str) -> str:
        query = urlencode({"league": league, "id": str(item_id)})
        return f"{self.base_url}/history?{query}"

    def fetch_history(
        self,
        league: str,
        item_id: int | str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        return self.http.get_json(
            self.history_url(league, item_id),
            etag=etag,
            last_modified=last_modified,
        )
