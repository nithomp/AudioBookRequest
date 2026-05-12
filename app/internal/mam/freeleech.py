"""
MaM (MyAnonamouse) freeleech browse client.

Fetches current freeleech audiobooks from MaM's torrent search endpoint
and caches the results for a configurable TTL.

The mam_id session cookie is read from the existing indexer configuration
(same key used by the MaM indexer in app/internal/indexers/mam.py).
"""

import json
import os
import re
import time
from datetime import datetime
from typing import Literal
from urllib.parse import quote_plus, urlencode, urljoin

from aiohttp import ClientSession
from pydantic import BaseModel, field_validator
from sqlmodel import Session

from app.internal.indexers.configuration import indexer_configuration_cache
from app.util.connection import USER_AGENT
from app.util.log import logger

# Optional HTTP proxy for MaM requests (e.g. route through gluetun VPN).
# Set ABR_MAM_HTTP_PROXY=http://gluetun:8888 in your environment.
_MAM_PROXY: str | None = os.environ.get("ABR_MAM_HTTP_PROXY") or None

MAM_BASE_URL = "https://www.myanonamouse.net"
MAM_SEARCH_PATH = "/tor/js/loadSearchJSONbasic.php"

# MaM main_cat ID for audiobooks
MAM_AUDIOBOOK_CAT = 13

FreeleechType = Literal["free", "fl_vip", "personal_freeleech", "vip"]


class MamFreeleechItem(BaseModel):
    """Represents a single freeleech torrent from MaM."""

    id: int
    title: str
    authors: list[str]
    narrators: list[str]
    size_bytes: int
    seeders: int
    leechers: int
    filetype: str
    catname: str
    added: datetime
    freeleech_types: list[FreeleechType]
    cover_url: str | None = None
    tags: list[str] = []
    # Enriched fields — populated from Audible metadata cache
    description: str | None = None
    genres: list[str] = []

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / 1_000_000, 1)

    @property
    def torrent_url(self) -> str:
        return f"{MAM_BASE_URL}/t/{self.id}"

    @property
    def audible_search_url(self) -> str:
        """Audible search URL for this title."""
        query = f"{self.title} {' '.join(self.authors[:1])}".strip()
        return f"https://www.audible.com/search?keywords={quote_plus(query)}"

    @property
    def freeleech_label(self) -> str:
        """Human-readable freeleech type label."""
        if "personal_freeleech" in self.freeleech_types:
            return "Personal FL"
        if "fl_vip" in self.freeleech_types:
            return "FL/VIP"
        if "free" in self.freeleech_types:
            return "Freeleech"
        if "vip" in self.freeleech_types:
            return "VIP"
        return "Freeleech"


def _parse_info_json(info: str | None) -> list[str]:
    """Parse MaM's stringified JSON object of id:name pairs."""
    if not info:
        return []
    try:
        content = json.loads(info)
        if isinstance(content, dict):
            return [v for v in content.values() if isinstance(v, str)]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _parse_size(size: str | int | None) -> int:
    """Parse size, which MaM returns as a string of bytes."""
    if size is None:
        return 0
    try:
        return int(size)
    except (ValueError, TypeError):
        return 0


def _parse_bool_field(value: str | int | bool | None) -> bool:
    """MaM returns booleans as "0"/"1" strings or actual booleans."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value) not in ("0", "false", "")


def _is_genre_tag(tag: str) -> bool:
    """
    Return True if a MaM tag looks like a genre/category rather than
    uploader metadata noise.

    MaM uploaders put all sorts of things in tags:
      - Proper genres: "Mystery", "Police Procedurals", "Science Fiction"
      - Description blobs: "1 M4b File | 07 Hrs | Released 2026-02-17 | Crime"
      - Dates: "02-12-26", "2026", "2026-01-01"
      - Audio specs: "128kbps | 44.1kHz | m4b"
      - Publisher metadata: "Release Date: February 10", "FORMAT Unabridged"

    We keep only short values that don't contain the above noise patterns.
    """
    tag = tag.strip().rstrip(".")
    if not tag or len(tag) > 36:
        return False
    # Pipe-separated blobs (e.g. "07 Hrs | Crime")
    if "|" in tag or "[" in tag:
        return False
    # Key:value metadata (e.g. "Release Date: Feb", "Length: 8 hrs")
    if ":" in tag:
        return False
    # Bare years (e.g. "2026", "2025")
    if re.fullmatch(r"\d{4}", tag):
        return False
    # Date-like starts (e.g. "02-12-26", "2026-01-01", "17/02/2026")
    if re.match(r"^\d{2}[\-/]", tag) or re.match(r"^\d{4}[\-/]", tag):
        return False
    # Audio/technical specs (e.g. "128kbps", "44.1kHz")
    if re.match(r"^\d+\s*(kbps|hz|khz|mb|gb|hrs?|min)", tag, re.IGNORECASE):
        return False
    # Lines starting with numbers that are clearly not genres
    if re.match(r"^\d+\s+[A-Z]", tag):
        return False
    # Obvious metadata keywords at start
    _META = ("release", "format", "language", "published", "length", "b0", "asin")
    if any(tag.lower().startswith(kw) for kw in _META):
        return False
    return True


def _parse_added(added: str | None) -> datetime:
    if not added:
        return datetime.now()
    try:
        return datetime.fromisoformat(added)
    except ValueError:
        try:
            # Try as unix timestamp
            return datetime.fromtimestamp(float(added))
        except (ValueError, TypeError):
            return datetime.now()


def _result_to_item(raw: dict) -> MamFreeleechItem | None:  # type: ignore[type-arg]
    """Convert a raw MaM search result dict to a MamFreeleechItem."""
    try:
        torrent_id = int(raw.get("id", 0))
        title = str(raw.get("name") or raw.get("title") or "")
        if not title or not torrent_id:
            return None

        freeleech_types: list[FreeleechType] = []
        if _parse_bool_field(raw.get("personal_freeleech")):
            freeleech_types.append("personal_freeleech")
        if _parse_bool_field(raw.get("fl_vip")):
            freeleech_types.append("fl_vip")
        if _parse_bool_field(raw.get("free")):
            freeleech_types.append("free")
        if _parse_bool_field(raw.get("vip")):
            freeleech_types.append("vip")

        # Cover image flag: non-zero "cover" field means MaM has art for this torrent.
        # The actual image is fetched client-side from Google Books (no auth needed).
        cover_available = bool(raw.get("cover") and str(raw.get("cover")) not in ("0", "false", ""))
        cover_url: str | None = "/freeleech/cover/{}".format(torrent_id) if cover_available else None

        # Tags: MaM returns a comma-separated list of uploader-supplied tags.
        # These are very noisy (description blobs, dates, specs), so we filter
        # to only short, genre-like values.
        raw_tags = raw.get("tags") or ""
        if isinstance(raw_tags, list):
            raw_tag_list = [str(t).strip() for t in raw_tags if str(t).strip()]
        elif isinstance(raw_tags, str) and raw_tags.strip():
            raw_tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
        else:
            raw_tag_list = []
        tags = [t for t in raw_tag_list if _is_genre_tag(t)]

        return MamFreeleechItem(
            id=torrent_id,
            title=title,
            authors=_parse_info_json(raw.get("author_info")),
            narrators=_parse_info_json(raw.get("narrator_info")),
            size_bytes=_parse_size(raw.get("size")),
            seeders=int(raw.get("seeders", 0) or 0),
            leechers=int(raw.get("leechers", 0) or 0),
            filetype=str(raw.get("filetype") or raw.get("filetypes") or ""),
            catname=str(raw.get("catname") or "Audiobook"),
            added=_parse_added(raw.get("added")),
            freeleech_types=freeleech_types,
            cover_url=cover_url,
            tags=tags,
        )
    except Exception as e:
        logger.debug("MaM freeleech: failed to parse result", error=str(e), raw=raw)
        return None


class MamFreeleechResult(BaseModel):
    items: list[MamFreeleechItem]
    fetched_at: datetime
    error: str | None = None

    @property
    def last_updated_str(self) -> str:
        return self.fetched_at.strftime("%H:%M:%S")


# In-memory cache: (fetched_at_ts, result)
_freeleech_cache: tuple[float, MamFreeleechResult] | None = None


def get_mam_session_id(session: Session) -> str | None:
    """Read mam_id from the indexer configuration cache (same key as MamIndexer)."""
    return indexer_configuration_cache.get(session, "mam_session_id")


def get_cached_result(ttl_seconds: int) -> MamFreeleechResult | None:
    """Return cached freeleech result if still fresh."""
    global _freeleech_cache
    if _freeleech_cache is None:
        return None
    cached_at, result = _freeleech_cache
    if time.time() - cached_at < ttl_seconds:
        return result
    return None


def flush_freeleech_cache():
    global _freeleech_cache
    _freeleech_cache = None


async def fetch_mam_freeleech(
    db_session: Session,
    client_session: ClientSession,
    ttl_seconds: int = 15 * 60,
    force_refresh: bool = False,
) -> MamFreeleechResult:
    """
    Fetch current freeleech audiobooks from MaM.

    Returns a cached result if one exists within the TTL, otherwise fetches fresh.
    If the mam_id cookie is not configured, returns an error result.
    """
    global _freeleech_cache

    if not force_refresh:
        cached = get_cached_result(ttl_seconds)
        if cached is not None:
            logger.debug("MaM freeleech: returning cached result")
            return cached

    mam_id = get_mam_session_id(db_session)
    if not mam_id:
        result = MamFreeleechResult(
            items=[],
            fetched_at=datetime.now(),
            error="MaM session ID (mam_id) is not configured. "
            "Set it in Settings > Indexers under the MyAnonamouse indexer.",
        )
        return result

    _PER_PAGE = 100
    _BASE_PARAMS = {
        "tor[main_cat][]": str(MAM_AUDIOBOOK_CAT),
        "tor[searchType]": "fl",
        "tor[searchIn]": "torrents",
        "perpage": str(_PER_PAGE),
    }
    _HEADERS = {"User-Agent": USER_AGENT}
    _COOKIES = {"mam_id": mam_id}

    raw_items: list[dict] = []  # type: ignore[type-arg]
    start = 0
    total_reported = None

    try:
        while True:
            params = {**_BASE_PARAMS, "startNumber": str(start)}
            url = urljoin(MAM_BASE_URL, MAM_SEARCH_PATH + "?" + urlencode(params, doseq=True))

            async with client_session.get(
                url,
                cookies=_COOKIES,
                headers=_HEADERS,
                proxy=_MAM_PROXY,
            ) as response:
                if response.status == 403:
                    body = await response.text()
                    logger.error("MaM freeleech: auth failed (403)", body=body[:200])
                    return MamFreeleechResult(
                        items=[],
                        fetched_at=datetime.now(),
                        error="MaM session cookie expired or invalid. "
                        "Update it in Settings > Indexers.",
                    )

                if not response.ok:
                    body = await response.text()
                    logger.error(
                        "MaM freeleech: request failed",
                        status=response.status,
                        body=body[:200],
                    )
                    return MamFreeleechResult(
                        items=[],
                        fetched_at=datetime.now(),
                        error=f"MaM returned HTTP {response.status}. Check your session cookie.",
                    )

                json_body = await response.json(content_type=None)  # type: ignore[assignment]

                if isinstance(json_body, dict) and "error" in json_body:
                    err_msg = str(json_body["error"])
                    logger.error("MaM freeleech: API error", error=err_msg)
                    return MamFreeleechResult(
                        items=[],
                        fetched_at=datetime.now(),
                        error=f"MaM API error: {err_msg}",
                    )

                # MaM returns `data` as either a list or a dict keyed by torrent ID
                raw_data = json_body.get("data", []) if isinstance(json_body, dict) else []
                if isinstance(raw_data, list):
                    page_data = raw_data
                elif isinstance(raw_data, dict):
                    page_data = list(raw_data.values())
                else:
                    page_data = []

                if total_reported is None:
                    # MaM uses "found" or "total" depending on endpoint version
                    for _key in ("found", "total", "total_items", "count"):
                        try:
                            val = int(json_body.get(_key) or 0)
                            if val > 0:
                                total_reported = val
                                break
                        except (ValueError, TypeError):
                            pass
                    if total_reported is None:
                        total_reported = 0

            raw_items.extend(page_data)
            logger.debug(
                "MaM freeleech: page fetched",
                start=start,
                page_count=len(page_data),
                total_so_far=len(raw_items),
                total_reported=total_reported,
            )

            # Stop if we got a short page or have reached the reported total
            if len(page_data) < _PER_PAGE:
                break
            if total_reported and len(raw_items) >= total_reported:
                break

            start += _PER_PAGE

    except Exception as e:
        logger.error("MaM freeleech: exception fetching data", error=str(e))
        return MamFreeleechResult(
            items=[],
            fetched_at=datetime.now(),
            error=f"Failed to connect to MaM: {e}",
        )

    items = []
    for raw in raw_items:
        item = _result_to_item(raw)
        if item is not None:
            items.append(item)

    logger.info("MaM freeleech: fetched results", count=len(items))

    # Apply any already-cached Audible metadata from the DB.
    # Full enrichment of uncached items is handled by the nightly scheduler
    # (see _freeleech_scheduler_loop in main.py) so we never block a user
    # request waiting for Audible API calls.
    try:
        from app.internal.mam.metadata import apply_cached_metadata  # avoid circular
        apply_cached_metadata(items, db_session)
    except Exception as _meta_exc:
        logger.warning("Freeleech meta: cache lookup failed", error=str(_meta_exc))

    result = MamFreeleechResult(items=items, fetched_at=datetime.now())
    _freeleech_cache = (time.time(), result)
    return result
