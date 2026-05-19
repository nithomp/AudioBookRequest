"""
Goodreads shelf RSS poller.

Fetches books from a Goodreads shelf RSS feed, searches Audible for a match,
creates an AudiobookRequest on behalf of the first admin user, and queues a
download via background_start_query.

No Goodreads API key required — only the public RSS feed URL.
"""

import asyncio
import html
import re
from datetime import datetime

import aiohttp
from sqlmodel import Session, select

from app.internal.goodreads.config import goodreads_config
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    GroupEnum,
    GoodreadsQueuedBook,
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
_RE_ITEMS   = re.compile(r'<item\b[^>]*>(.*?)</item>', re.DOTALL | re.IGNORECASE)
_RE_BOOK_ID = re.compile(r'<book_id>\s*(\d+)\s*</book_id>', re.IGNORECASE)
_RE_BOOK_ID_NESTED = re.compile(r'<book\b[^>]*>.*?<id>\s*(\d+)\s*</id>', re.DOTALL | re.IGNORECASE)
_RE_AUTHOR  = re.compile(r'<author_name>(.*?)</author_name>', re.DOTALL | re.IGNORECASE)
_RE_TITLE_NO_SERIES = re.compile(r'<title_without_series>(.*?)</title_without_series>', re.DOTALL | re.IGNORECASE)
_RE_BOOK_TITLE = re.compile(r'<book\b[^>]*>.*?<title>(.*?)</title>', re.DOTALL | re.IGNORECASE)
_RE_ITEM_TITLE = re.compile(r'<title>(.*?)</title>', re.DOTALL | re.IGNORECASE)


def _text(m: re.Match | None) -> str:  # type: ignore[type-arg]
    """Return stripped, HTML-unescaped text from a regex match group, or ''."""
    if m is None:
        return ""
    return html.unescape(m.group(1)).strip()


def _parse_items(xml_text: str) -> list[dict]:  # type: ignore[type-arg]
    """
    Extract book items from a Goodreads RSS feed using regex rather than an
    XML parser.  Goodreads embeds raw HTML in several fields which makes the
    feed not well-formed XML, so ElementTree reliably chokes on it.
    We only need book_id, title, and author_name — all of which are plain text.
    """
    results = []
    for item_m in _RE_ITEMS.finditer(xml_text):
        body = item_m.group(1)

        # book_id — try direct tag first, then nested <book><id>
        book_id = _text(_RE_BOOK_ID.search(body))
        if not book_id:
            book_id = _text(_RE_BOOK_ID_NESTED.search(body))
        if not book_id:
            continue

        author = _text(_RE_AUTHOR.search(body))

        # Title: prefer title_without_series, then <book><title>, then <title>
        title = (
            _text(_RE_TITLE_NO_SERIES.search(body))
            or _text(_RE_BOOK_TITLE.search(body))
        )
        if not title:
            raw = _text(_RE_ITEM_TITLE.search(body))
            # Goodreads sometimes formats item title as "Book Title by Author Name"
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
    """
    Search Audible for `title author` and return the ASIN of the best match,
    or None if nothing is found.
    """
    from app.internal.audible.search import search_audible_books

    query = f"{title} {author}".strip()
    async with _AUDIBLE_CONCURRENCY:
        try:
            books = await search_audible_books(cs, query, num_results=5)
        except Exception as exc:
            logger.warning(
                "Goodreads poller: Audible search failed",
                query=query,
                error=str(exc),
            )
            return None

    if not books:
        return None
    return books[0].asin


def _get_admin_username(db: Session) -> str | None:
    """Return the username of the root user, falling back to any admin."""
    root_user = db.exec(select(User).where(User.root == True)).first()  # noqa: E712
    if root_user:
        return root_user.username
    admin_user = db.exec(
        select(User).where(User.group == GroupEnum.admin)
    ).first()
    return admin_user.username if admin_user else None


async def _process_book(
    cs: aiohttp.ClientSession,
    book_id: str,
    title: str,
    author: str,
    auto_download: bool,
) -> str:
    """
    Match one Goodreads book against Audible, create a request, and queue it.
    Returns a status string: 'queued' | 'not_found' | 'already_tracked' | 'error'.
    """
    from app.internal.query import background_start_query

    try:
        with next(get_session()) as db:
            # Already tracked?
            existing = db.get(GoodreadsQueuedBook, book_id)
            if existing:
                return "already_tracked"

        asin = await _find_audible_asin(cs, title, author)

        with next(get_session()) as db:
            if not asin:
                db.add(
                    GoodreadsQueuedBook(
                        goodreads_book_id=book_id,
                        title=title,
                        author=author,
                        status="not_found",
                    )
                )
                db.commit()
                return "not_found"

            # Ensure Audiobook exists in DB
            db_book = db.get(Audiobook, asin)
            if not db_book:
                from app.internal.audible.single import get_single_book

                try:
                    fetched = await get_single_book(cs, asin=asin)
                    if fetched:
                        db.add(fetched)
                        db.commit()
                except Exception as exc:
                    logger.warning(
                        "Goodreads poller: could not fetch full Audible record",
                        asin=asin,
                        error=str(exc),
                    )
                    # Still proceed — the request can be created,
                    # and background_start_query will fetch if needed

            admin_username = _get_admin_username(db)
            if not admin_username:
                logger.warning(
                    "Goodreads poller: no admin user found, cannot create request",
                    title=title,
                )
                return "error"

            # Create request if it doesn't exist yet
            existing_req = db.exec(
                select(AudiobookRequest).where(
                    AudiobookRequest.asin == asin,
                    AudiobookRequest.user_username == admin_username,
                )
            ).first()
            if not existing_req:
                db.add(AudiobookRequest(asin=asin, user_username=admin_username))
                db.commit()

            # Record in our tracking table
            db.add(
                GoodreadsQueuedBook(
                    goodreads_book_id=book_id,
                    title=title,
                    author=author,
                    asin=asin,
                    status="queued",
                )
            )
            db.commit()

        # Fire off the download query in the background
        await background_start_query(asin, auto_download=auto_download)
        return "queued"

    except Exception as exc:
        logger.error(
            "Goodreads poller: unexpected error processing book",
            book_id=book_id,
            title=title,
            error=str(exc),
        )
        return "error"


async def poll_goodreads_shelf(db: Session) -> dict:
    """
    Poll the configured Goodreads RSS shelf URL.
    Returns a summary dict: {queued, already_tracked, not_found, errors, error (str|None)}.
    """
    summary: dict = {
        "queued": 0,
        "already_tracked": 0,
        "not_found": 0,
        "errors": 0,
        "error": None,
    }

    rss_url = goodreads_config.get_rss_url(db)
    if not rss_url:
        summary["error"] = "No Goodreads RSS URL configured."
        return summary

    auto_download = goodreads_config.get_auto_download(db)

    logger.info("Goodreads poller: fetching RSS", url=rss_url)

    try:
        async with aiohttp.ClientSession(
            timeout=_POLL_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as cs:
            async with cs.get(rss_url) as resp:
                if resp.status != 200:
                    summary["error"] = (
                        f"Goodreads returned HTTP {resp.status}. "
                        "Check the RSS URL in Settings > Goodreads."
                    )
                    return summary
                xml_text = await resp.text()

        items = _parse_items(xml_text)
        if not items:
            logger.info("Goodreads poller: no items found in RSS feed")
            goodreads_config.set_last_polled(db, datetime.now().isoformat())
            return summary

        logger.info("Goodreads poller: processing items", count=len(items))

        async with aiohttp.ClientSession(
            timeout=_AUDIBLE_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as audible_cs:
            tasks = [
                _process_book(
                    audible_cs,
                    item["book_id"],
                    item["title"],
                    item["author"],
                    auto_download,
                )
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

        goodreads_config.set_last_polled(db, datetime.now().isoformat())
        logger.info("Goodreads poller: done", **{k: v for k, v in summary.items() if k != "error"})

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Goodreads poller: fatal error", error=str(exc))
        summary["error"] = f"Poll failed: {exc}"

    return summary
