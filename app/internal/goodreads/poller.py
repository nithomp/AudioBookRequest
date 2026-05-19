"""
Goodreads shelf RSS poller.

Each user can configure their own shelf RSS URL.  The scheduler calls
poll_all_users() which iterates every user with a configured URL and polls
their shelf, creating AudiobookRequests on their behalf.

No Goodreads API key required — only the public RSS feed URL.
"""

import asyncio
import html
import re
from datetime import datetime

import aiohttp
from sqlmodel import Session, select

from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    GoodreadsQueuedBook,
    GoodreadsUserConfig,
    User,
)
from app.util.connection import USER_AGENT
from app.util.db import get_session
from app.util.log import logger

_POLL_TIMEOUT = aiohttp.ClientTimeout(total=30)
_AUDIBLE_TIMEOUT = aiohttp.ClientTimeout(total=15)
_AUDIBLE_CONCURRENCY = asyncio.Semaphore(3)


# Regex patterns for extracting fields directly from RSS text.
# We bypass the XML parser entirely because Goodreads RSS embeds raw HTML
# (unclosed tags, bare &, etc.) in multiple fields, making it not well-formed XML.
_RE_ITEMS          = re.compile(r'<item\b[^>]*>(.*?)</item>', re.DOTALL | re.IGNORECASE)
_RE_BOOK_ID        = re.compile(r'<book_id>\s*(\d+)\s*</book_id>', re.IGNORECASE)
_RE_BOOK_ID_NESTED = re.compile(r'<book\b[^>]*>.*?<id>\s*(\d+)\s*</id>', re.DOTALL | re.IGNORECASE)
_RE_AUTHOR         = re.compile(r'<author_name>(.*?)</author_name>', re.DOTALL | re.IGNORECASE)
_RE_TITLE_NO_SERIES = re.compile(r'<title_without_series>(.*?)</title_without_series>', re.DOTALL | re.IGNORECASE)
_RE_BOOK_TITLE     = re.compile(r'<book\b[^>]*>.*?<title>(.*?)</title>', re.DOTALL | re.IGNORECASE)
_RE_ITEM_TITLE     = re.compile(r'<title>(.*?)</title>', re.DOTALL | re.IGNORECASE)
_RE_CDATA          = re.compile(r'<!\[CDATA\[(.*?)]]>', re.DOTALL)


def _text(m: re.Match | None) -> str:  # type: ignore[type-arg]
    """Return stripped, HTML-unescaped text from a regex match group, or ''."""
    if m is None:
        return ""
    raw = m.group(1).strip()
    cdata = _RE_CDATA.search(raw)
    if cdata:
        raw = cdata.group(1).strip()
    return html.unescape(raw).strip()


def _parse_items(xml_text: str) -> list[dict]:  # type: ignore[type-arg]
    """
    Extract book items from a Goodreads RSS feed using regex rather than an
    XML parser.  Goodreads embeds raw HTML in several fields which makes the
    feed not well-formed XML.  We only need book_id, title, author_name.
    """
    results = []
    for item_m in _RE_ITEMS.finditer(xml_text):
        body = item_m.group(1)

        book_id = _text(_RE_BOOK_ID.search(body))
        if not book_id:
            book_id = _text(_RE_BOOK_ID_NESTED.search(body))
        if not book_id:
            continue

        author = _text(_RE_AUTHOR.search(body))

        title = (
            _text(_RE_TITLE_NO_SERIES.search(body))
            or _text(_RE_BOOK_TITLE.search(body))
        )
        if not title:
            raw = _text(_RE_ITEM_TITLE.search(body))
            if " by " in raw:
                parts = raw.rsplit(" by ", 1)
                title = parts[0].strip()
                if not author:
                    author = parts[1].strip()
            else:
                title = raw

        if title:
            results.append({"book_id": book_id, "title": title, "author": author})
            logger.debug(
                "Goodreads poller: parsed item",
                book_id=book_id,
                title=title,
                author=author,
            )

    return results


async def _find_audible_asin(
    cs: aiohttp.ClientSession,
    title: str,
    author: str,
) -> str | None:
    from app.internal.audible.search import search_audible_books

    query = f"{title} {author}".strip()
    async with _AUDIBLE_CONCURRENCY:
        try:
            books = await search_audible_books(cs, query, num_results=5)
        except Exception as exc:
            logger.warning("Goodreads poller: Audible search failed", query=query, error=str(exc))
            return None

    return books[0].asin if books else None


async def _process_book(
    cs: aiohttp.ClientSession,
    book_id: str,
    title: str,
    author: str,
    username: str,
    auto_download: bool,
) -> str:
    """
    Match one book against Audible, create a request for `username`, queue it.
    Returns: 'queued' | 'not_found' | 'already_tracked' | 'error'
    """
    from app.internal.query import background_start_query

    try:
        with next(get_session()) as db:
            existing = db.get(GoodreadsQueuedBook, (book_id, username))
            if existing:
                return "already_tracked"

        asin = await _find_audible_asin(cs, title, author)

        with next(get_session()) as db:
            if not asin:
                db.add(GoodreadsQueuedBook(
                    goodreads_book_id=book_id,
                    username=username,
                    title=title,
                    author=author,
                    status="not_found",
                ))
                db.commit()
                return "not_found"

            # Ensure Audiobook exists in DB
            if not db.get(Audiobook, asin):
                from app.internal.audible.single import get_single_book
                try:
                    fetched = await get_single_book(cs, asin=asin)
                    if fetched:
                        db.add(fetched)
                        db.commit()
                except Exception as exc:
                    logger.warning("Goodreads poller: could not fetch Audible record",
                                   asin=asin, error=str(exc))

            # Create AudiobookRequest for this user if not already present
            existing_req = db.exec(
                select(AudiobookRequest).where(
                    AudiobookRequest.asin == asin,
                    AudiobookRequest.user_username == username,
                )
            ).first()
            if not existing_req:
                db.add(AudiobookRequest(asin=asin, user_username=username))
                db.commit()

            db.add(GoodreadsQueuedBook(
                goodreads_book_id=book_id,
                username=username,
                title=title,
                author=author,
                asin=asin,
                status="queued",
            ))
            db.commit()

        await background_start_query(asin, auto_download=auto_download)
        return "queued"

    except Exception as exc:
        logger.error("Goodreads poller: error processing book",
                     book_id=book_id, title=title, username=username, error=str(exc))
        return "error"


async def poll_user_shelf(username: str, rss_url: str, auto_download: bool) -> dict:  # type: ignore[type-arg]
    """Poll one user's Goodreads shelf. Returns a summary dict."""
    summary = {"queued": 0, "already_tracked": 0, "not_found": 0, "errors": 0, "error": None}

    logger.info("Goodreads poller: fetching RSS", url=rss_url, username=username)

    try:
        async with aiohttp.ClientSession(
            timeout=_POLL_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as cs:
            async with cs.get(rss_url) as resp:
                if resp.status != 200:
                    summary["error"] = f"Goodreads returned HTTP {resp.status}."
                    return summary
                xml_text = await resp.text()

        raw_item_count = xml_text.lower().count('<item')
        items = _parse_items(xml_text)
        logger.debug(
            "Goodreads poller: feed parsed",
            username=username,
            raw_item_tags=raw_item_count,
            parsed_items=len(items),
        )
        if not items:
            logger.info("Goodreads poller: no items found in RSS feed", username=username)
            _update_last_polled(username)
            return summary

        logger.info("Goodreads poller: processing items", count=len(items), username=username)

        async with aiohttp.ClientSession(
            timeout=_AUDIBLE_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as audible_cs:
            tasks = [
                _process_book(audible_cs, item["book_id"], item["title"],
                               item["author"], username, auto_download)
                for item in items
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                summary["errors"] += 1
            elif res == "queued":
                summary["queued"] += 1
            elif res == "already_tracked":
                summary["already_tracked"] += 1
            elif res == "not_found":
                summary["not_found"] += 1
            else:
                summary["errors"] += 1

        _update_last_polled(username)
        logger.info("Goodreads poller: done", username=username,
                    **{k: v for k, v in summary.items() if k != "error"})

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Goodreads poller: fatal error", username=username, error=str(exc))
        summary["error"] = str(exc)

    return summary


def _update_last_polled(username: str) -> None:
    try:
        with next(get_session()) as db:
            cfg = db.get(GoodreadsUserConfig, username)
            if cfg:
                cfg.last_polled = datetime.now().isoformat()
                db.add(cfg)
                db.commit()
    except Exception as exc:
        logger.warning("Goodreads poller: could not update last_polled",
                       username=username, error=str(exc))


async def poll_all_users() -> None:
    """Poll the Goodreads shelf for every user who has a URL configured."""
    with next(get_session()) as db:
        configs = db.exec(
            select(GoodreadsUserConfig).where(GoodreadsUserConfig.rss_url != "")
        ).all()

    if not configs:
        logger.debug("Goodreads poller: no users configured, skipping")
        return

    for cfg in configs:
        try:
            await poll_user_shelf(cfg.username, cfg.rss_url, cfg.auto_download)
        except Exception as exc:
            logger.error("Goodreads poller: failed for user",
                         username=cfg.username, error=str(exc))
