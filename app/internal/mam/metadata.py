"""
Server-side Audible metadata enrichment for MaM freeleech books.

Caches cover art, descriptions, and genres in the FreeleechBookMeta table
so that the freeleech page renders instantly without any client-side API calls.

Cache TTL per book entry: 30 days.
Background enrichment: uncached books are fetched from Audible in a throttled
asyncio task that runs after the page response is returned to the user.
"""

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING

import aiohttp
from pydantic import BaseModel
from sqlmodel import Session, select

from app.internal.audible.types import audible_regions, get_region_from_settings
from app.internal.models import FreeleechBookMeta
from app.util.log import logger

if TYPE_CHECKING:
    from app.internal.mam.freeleech import MamFreeleechItem

# Per-book TTL: 30 days
_META_TTL: int = 30 * 24 * 60 * 60

# Max concurrent Audible requests during background enrichment
_CONCURRENCY: int = 5


# ── Lookup key ────────────────────────────────────────────────────────────────

def _book_key(title: str, author: str) -> str:
    """Stable 16-char cache key: MD5(lower(title)|lower(author))[:16]."""
    raw = f"{title.lower().strip()}|{author.lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_get(session: Session, key: str) -> FreeleechBookMeta | None:
    return session.exec(
        select(FreeleechBookMeta).where(FreeleechBookMeta.lookup_key == key)
    ).one_or_none()


def _db_set(
    session: Session,
    key: str,
    cover_url: str | None,
    description: str | None,
    genres: list[str],
) -> None:
    row = _db_get(session, key)
    if row is None:
        row = FreeleechBookMeta(lookup_key=key)
    row.cover_url = cover_url
    row.description = description
    row.genres_json = json.dumps(genres)
    row.fetched_at = time.time()
    session.add(row)
    session.commit()


# ── Audible enrichment API ────────────────────────────────────────────────────

class _AudibleLadder(BaseModel):
    class _Rung(BaseModel):
        name: str

    ladder: list[_Rung] = []


class _AudibleEnrichProduct(BaseModel):
    asin: str
    title: str
    product_images: dict[str, str] = {}
    publisher_summary: str | None = None
    category_ladders: list[_AudibleLadder] = []

    @property
    def cover_url(self) -> str | None:
        img = self.product_images.get("500")
        if not img:
            vals = list(self.product_images.values())
            img = vals[0] if vals else None
        return img

    @property
    def genres(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        _SKIP = {"Audiobook", "Audiobooks", "Audio CD", "Unabridged"}
        for ladder in self.category_ladders:
            for rung in ladder.ladder:
                name = rung.name.strip()
                if name and name not in seen and name not in _SKIP:
                    seen.add(name)
                    result.append(name)
        return result


class _AudibleEnrichResponse(BaseModel):
    products: list[_AudibleEnrichProduct] = []


async def _fetch_audible_meta(
    client_session: aiohttp.ClientSession,
    title: str,
    author: str,
) -> tuple[str | None, str | None, list[str]]:
    """
    Query Audible for cover, description, and genres for one book.
    Returns (cover_url, description, genres). Never raises.
    """
    region = get_region_from_settings()
    tld = audible_regions[region]
    url = f"https://api.audible{tld}/1.0/catalog/products"
    query = f"{title} {author}".strip()
    params = {
        "num_results": 5,
        "products_sort_by": "Relevance",
        "keywords": query,
        "response_groups": "media,product_desc,category_ladders",
    }
    try:
        async with client_session.get(url, params=params) as resp:
            if resp.status != 200:
                return None, None, []
            data = await resp.json(content_type=None)
            parsed = _AudibleEnrichResponse.model_validate(data)
        if not parsed.products:
            return None, None, []
        product = parsed.products[0]
        return product.cover_url, product.publisher_summary, product.genres
    except Exception as exc:
        logger.debug("Freeleech meta: Audible fetch failed", title=title, error=str(exc))
        return None, None, []


# ── Phase 1: sync cache application ──────────────────────────────────────────

def apply_cached_metadata(
    items: "list[MamFreeleechItem]",
    session: Session,
) -> "list[MamFreeleechItem]":
    """
    Apply cached Audible metadata to items synchronously.
    Returns items that still need enrichment (not in cache / cache stale).
    Never raises — any DB error causes all items to be returned as uncached.
    """
    try:
        needs_enrichment: list[MamFreeleechItem] = []
        for item in items:
            author = item.authors[0] if item.authors else ""
            key = _book_key(item.title, author)
            row = _db_get(session, key)

            if row is not None and (time.time() - row.fetched_at) < _META_TTL:
                # Cache hit — apply metadata immediately
                item.cover_url = row.cover_url or item.cover_url
                item.description = row.description
                item.genres = json.loads(row.genres_json)
            else:
                needs_enrichment.append(item)

        return needs_enrichment
    except Exception as exc:
        logger.warning(
            "Freeleech meta: cache lookup failed (table may not exist yet)",
            error=str(exc),
        )
        return items  # treat all as uncached


# ── Phase 2: background enrichment ───────────────────────────────────────────

async def enrich_background(
    items: "list[MamFreeleechItem]",
) -> None:
    """
    Background task: fetch Audible metadata for a batch of items.
    Uses a single shared aiohttp session, throttled by a semaphore.
    Never raises — all exceptions are logged and suppressed so uvicorn stays up.
    """
    from app.util.db import get_session  # local import to avoid circular deps

    if not items:
        return

    sem = asyncio.Semaphore(_CONCURRENCY)

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as cs:

            async def fetch_one(item: "MamFreeleechItem") -> None:
                try:
                    author = item.authors[0] if item.authors else ""
                    key = _book_key(item.title, author)

                    async with sem:
                        cover_url, description, genres = await _fetch_audible_meta(
                            cs, item.title, author
                        )

                    # Persist to DB
                    try:
                        with next(get_session()) as db:
                            _db_set(db, key, cover_url, description, genres)
                    except Exception as db_exc:
                        logger.debug(
                            "Freeleech meta: DB write failed",
                            title=item.title,
                            error=str(db_exc),
                        )

                    # Patch item in-place so the in-memory cache benefits too
                    if cover_url:
                        item.cover_url = cover_url
                    item.description = description
                    item.genres = genres

                except Exception as exc:
                    logger.debug(
                        "Freeleech meta: fetch_one failed",
                        title=getattr(item, "title", "?"),
                        error=str(exc),
                    )

            # return_exceptions=True: collect failures instead of raising
            results = await asyncio.gather(
                *[fetch_one(item) for item in items],
                return_exceptions=True,
            )
            failed = sum(1 for r in results if isinstance(r, BaseException))
            logger.info(
                "Freeleech meta: background enrichment complete",
                total=len(items),
                failed=failed,
            )

    except Exception as exc:
        logger.error(
            "Freeleech meta: background enrichment task error",
            error=str(exc),
        )
