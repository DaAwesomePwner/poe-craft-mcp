"""Pure helpers for the trade client: saved-URL parsing, query hashing, listing cleanup.

Recorded responses live in `tests/fixtures/trade/`. Search ids are gzip-compressed,
base64url-encoded query JSON (`H4sI...`), so most saved URLs decode without a request.

Pricing uses only instant-buyout (asynchronous trade) listings: every search is sent with
status `securable`, and a fetched listing counts as instant buyout only when it carries the
async-trade gold `fee`, which whisper listings never have (checked live, 2026-11-23).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import zlib
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from poe_craft_mcp.log import mask_secrets

Kind = Literal["search", "exchange"]
SAVED_URL_RE = re.compile(r"^/trade/(search|exchange)/([^/]+)/([^/]+)/?$")
SEARCH_ID_RE = re.compile(r"[A-Za-z0-9_=-]+", re.ASCII)
GZIP_ID_PREFIX = "H4sI"  # base64 of the gzip magic bytes 1f 8b 08
MAX_SEARCH_ID_LEN = 8192
MAX_QUERY_BYTES = 64 * 1024
STATUS_INSTANT_BUYOUT = "securable"
EXCHANGE_REFUSED = "exchange listings are whisper-based; use an item search"


class TradeError(Exception):
    """A trade request failed for a reason other than rate limiting. The message is
    secret-masked, since it can quote the server's error text."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(mask_secrets(message))
        self.status = status


@dataclass(frozen=True)
class SavedSearch:
    """A trade URL split into its parts; the URL's league is kept only for reference."""

    kind: Kind
    url_league: str
    search_id: str


@dataclass(frozen=True)
class TradeQuery:
    """A query ready to post to any league: `{"query": ..., "sort": ...}` body parts."""

    kind: Kind
    query: dict[str, Any]
    sort: dict[str, Any] | None = None


@dataclass(frozen=True)
class SearchResult:
    id: str
    result: list[str]
    total: int


def parse_saved_url(url: str) -> SavedSearch:
    parts = urlsplit(url.strip())
    match = SAVED_URL_RE.match(parts.path)
    if parts.scheme not in ("http", "https") or match is None:
        raise TradeError("not a pathofexile.com/trade/search or /trade/exchange URL")
    kind, league, search_id = match.groups()
    search_id = unquote(search_id)
    if len(search_id) > MAX_SEARCH_ID_LEN or not SEARCH_ID_RE.fullmatch(search_id):
        raise TradeError("trade URL has a malformed or oversized search id")
    return SavedSearch(kind, unquote(league), search_id)  # type: ignore[arg-type]


def decode_search_id(search_id: str) -> dict[str, Any] | None:
    """The query JSON embedded in a gzip+base64url id, or None for an opaque id.

    Input and decompressed sizes are bounded, so a crafted id cannot exhaust memory.
    """
    if not search_id.startswith(GZIP_ID_PREFIX):
        return None
    if len(search_id) > MAX_SEARCH_ID_LEN:
        raise TradeError("saved search id is too long")
    try:
        packed = base64.urlsafe_b64decode(search_id + "=" * (-len(search_id) % 4))
        inflater = zlib.decompressobj(wbits=31)
        raw = inflater.decompress(packed, MAX_QUERY_BYTES)
        if inflater.unconsumed_tail or not inflater.eof:
            raise TradeError("saved search query is too large or truncated")
        data = json.loads(raw)
    except (binascii.Error, ValueError, zlib.error):
        raise TradeError("saved search id is not a valid compressed query") from None
    if not isinstance(data, dict):
        raise TradeError("saved search query is not a JSON object")
    return data


def query_from_body(body: Any, kind: Kind) -> TradeQuery:
    """Accept `{"query": ..., "sort": ...}` or a bare query object."""
    if not isinstance(body, dict):
        raise TradeError("trade query must be a JSON object")
    if isinstance(body.get("query"), dict):
        sort = body.get("sort")
        return TradeQuery(kind, body["query"], sort if isinstance(sort, dict) else None)
    return TradeQuery(kind, body)


def with_instant_buyout(query: dict[str, Any]) -> dict[str, Any]:
    """A copy of `query` restricted to instant-buyout listings, whatever status it had."""
    out = json.loads(json.dumps(query))
    out["status"] = {"option": STATUS_INSTANT_BUYOUT}
    return out


def contains_text(value: Any, needle: str) -> bool:
    """True if `needle` occurs in any string key or value of decoded JSON."""
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(contains_text(k, needle) or contains_text(v, needle) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_text(v, needle) for v in value)
    return False


def query_hash(*parts: Any) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def is_instant_buyout(entry: Any) -> bool:
    """True when a fetch result is an instant-buyout listing (it has the async-trade fee)."""
    listing = entry.get("listing") if isinstance(entry, dict) else None
    fee = listing.get("fee") if isinstance(listing, dict) else None
    return isinstance(fee, int | float) and not isinstance(fee, bool)


def normalize_listing(entry: Any) -> dict[str, Any] | None:
    """One fetch result -> `{id, amount, currency, indexed, stack_size}`.

    Returns None for delisted (null) entries and listings without a price. Account and
    character names are dropped.
    """
    if not isinstance(entry, dict):
        return None
    listing = entry.get("listing") or {}
    price = listing.get("price") or {}
    if price.get("amount") is None or not price.get("currency"):
        return None
    item = entry.get("item") or {}
    return {
        "id": entry.get("id"),
        "amount": float(price["amount"]),
        "currency": price["currency"],
        "indexed": listing.get("indexed"),
        "stack_size": item.get("stackSize"),
    }
