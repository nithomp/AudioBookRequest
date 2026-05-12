import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, cast
from urllib.parse import quote_plus, urlencode

import aiohttp
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware import Middleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlmodel import select
from starlette.responses import Content

from app.internal.audible.search import clear_old_book_caches
from app.internal.auth.authentication import RequiresLoginException
from app.internal.auth.config import auth_config, initialize_force_login_type
from app.internal.auth.oidc_config import InvalidOIDCConfiguration
from app.internal.auth.session_middleware import (
    DynamicSessionMiddleware,
    middleware_linker,
)
from app.internal.env_settings import Settings
from app.internal.models import User
from app.internal.prowlarr.util import ProwlarrMisconfigured
from app.routers import api, pages
from app.util.db import get_session
from app.util.fetch_js import fetch_scripts
from app.util.log import logger
from app.util.redirect import BaseUrlRedirectResponse
from app.util.templates import catalog_response
from app.util.toast import ToastException

# intialize js dependencies or throw an error if not in debug mode
fetch_scripts(Settings().app.debug)

with next(get_session()) as session:
    auth_secret = auth_config.get_auth_secret(session)
    initialize_force_login_type(session)
    clear_old_book_caches(session)


# ── Freeleech background scheduler ───────────────────────────────────────────

async def _run_freeleech_refresh() -> None:
    """
    Fetch the current MaM freeleech list and warm the Audible metadata cache.
    New items (not already in FreeleechBookMeta) are enriched via Audible.
    Items already cached are served instantly with no extra API calls.
    """
    from app.internal.mam.config import mam_freeleech_config
    from app.internal.mam.freeleech import fetch_mam_freeleech

    logger.info("Freeleech scheduler: starting refresh")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as client:
            with next(get_session()) as db:
                ttl = mam_freeleech_config.get_ttl_seconds(db)
                result = await fetch_mam_freeleech(
                    db_session=db,
                    client_session=client,
                    ttl_seconds=ttl,
                    force_refresh=True,
                )

        if result.error:
            logger.warning(
                "Freeleech scheduler: MaM returned an error — will retry tomorrow",
                error=result.error,
            )
            return

        logger.info(
            "Freeleech scheduler: MaM fetch complete, enriching metadata",
            count=len(result.items),
        )

        # Determine which items still need Audible enrichment
        from app.internal.mam.metadata import apply_cached_metadata, enrich_background
        with next(get_session()) as db:
            needs_enrichment = apply_cached_metadata(result.items, db)

        if needs_enrichment:
            logger.info(
                "Freeleech scheduler: enriching uncached items via Audible",
                count=len(needs_enrichment),
            )
            # await here — scheduler can afford to wait, users cannot
            await enrich_background(needs_enrichment)
        else:
            logger.info("Freeleech scheduler: all metadata already cached, nothing to enrich")

        logger.info("Freeleech scheduler: nightly refresh complete", total=len(result.items))

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Freeleech scheduler: refresh failed", error=str(exc))


async def _freeleech_scheduler_loop() -> None:
    """
    Runs forever as a background asyncio task.
    Sleeps until the next Monday at 02:00 UTC, then runs the freeleech refresh.
    MaM freeleech periods are ~2 weeks so a weekly cadence catches any new
    list within a week without hammering their servers.
    """
    # Short startup delay — let the app fully initialise first
    await asyncio.sleep(10)

    while True:
        now = datetime.now(timezone.utc)
        # Find the next Monday 02:00 UTC (weekday 0 = Monday)
        days_until_monday = (7 - now.weekday()) % 7 or 7  # always at least 1 day ahead
        next_run = (now + timedelta(days=days_until_monday)).replace(
            hour=2, minute=0, second=0, microsecond=0
        )
        sleep_secs = (next_run - now).total_seconds()
        logger.info(
            "Freeleech scheduler: next run scheduled",
            next_run_utc=next_run.strftime("%Y-%m-%d %H:%M UTC"),
            sleep_days=round(sleep_secs / 86400, 1),
        )
        await asyncio.sleep(sleep_secs)
        await _run_freeleech_refresh()


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    task = asyncio.create_task(
        _freeleech_scheduler_loop(), name="freeleech-scheduler"
    )
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="AudioBookRequest",
    lifespan=lifespan,
    debug=Settings().app.debug,
    openapi_url="/openapi.json" if Settings().app.openapi_enabled else None,
    description="API for AudiobookRequest",
    middleware=[
        Middleware(DynamicSessionMiddleware, auth_secret, middleware_linker),
        Middleware(GZipMiddleware),
    ],
    root_path=Settings().app.base_url.rstrip("/"),
    redirect_slashes=False,
)

app.include_router(pages.router, include_in_schema=False)
app.include_router(api.router)

user_exists = False


@app.exception_handler(RequiresLoginException)
async def redirect_to_login(request: Request, exc: RequiresLoginException):
    if request.method == "GET":
        params: dict[str, str] = {}
        if exc.detail:
            params["error"] = exc.detail
        path = request.url.path.removeprefix(Settings().app.base_url.rstrip("/"))
        if path != "/" and not path.startswith("/login"):
            params["redirect_uri"] = path
        return BaseUrlRedirectResponse("/login?" + urlencode(params))
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.exception_handler(InvalidOIDCConfiguration)
async def redirect_to_invalid_oidc(request: Request, exc: InvalidOIDCConfiguration):
    _ = request
    path = "/auth/oidc/invalid"
    if exc.detail:
        path += f"?error={quote_plus(exc.detail)}"
    if request.headers.get("HX-Request"):
        return Response(status_code=400, headers={"HX-Redirect": path})
    return BaseUrlRedirectResponse(path)


@app.exception_handler(ProwlarrMisconfigured)
async def redirect_to_invalid_prowlarr(request: Request, exc: ProwlarrMisconfigured):
    _ = exc
    path = "/settings/invalid?prowlarr_misconfigured=true"
    if request.headers.get("HX-Request"):
        return Response(status_code=400, headers={"HX-Redirect": path})
    return BaseUrlRedirectResponse(path)


@app.exception_handler(ToastException)
async def raise_toast(request: Request, exc: ToastException):
    _ = request
    toast_error = toast_success = toast_info = None
    if exc.type == "error":
        toast_error = exc.message
    elif exc.type == "success":
        toast_success = exc.message
    elif exc.type == "info":
        toast_info = exc.message

    return catalog_response(
        "ToastBlock",
        toast_error=toast_error,
        toast_success=toast_success,
        toast_info=toast_info,
        headers={"HX-Retarget": "#toast-block"}
        | ({"HX-Refresh": "true"} if exc.force_refresh else {}),
    )


@app.middleware("http")
async def redirect_to_init(
    request: Request,
    call_next: Callable[[Request], Awaitable[StreamingResponse]],
):
    """
    Initial redirect if no user exists. We force the user to create a new login
    """
    global user_exists
    path = request.url.path.removeprefix(Settings().app.base_url.rstrip("/"))
    if (
        not user_exists
        and path != "/init"
        and not path.startswith("/static")
        and request.method == "GET"
    ):
        with next(get_session()) as session:
            user_count = session.exec(select(func.count()).select_from(User)).one()
            if user_count == 0:
                return BaseUrlRedirectResponse("/init")
            else:
                user_exists = True
    elif user_exists and path.startswith("/init"):
        return BaseUrlRedirectResponse("/")
    response = await call_next(request)
    return response


@app.middleware("http")
async def throw_toast_exception(
    request: Request,
    call_next: Callable[[Request], Awaitable[StreamingResponse]],
):
    """On htmx requests, convert HTTPExceptions/other errors into ToastExceptions"""
    response = await call_next(request)
    if (
        400 <= response.status_code
        and not response.headers.get("HX-Redirect")  # already handled
        and request.headers.get("HX-Request") == "true"
    ):

        def to_string(b: Content) -> str:
            if isinstance(b, bytes):
                return b.decode("utf-8")
            elif isinstance(b, str):
                return b
            else:
                return str(b)

        body = "".join([to_string(x) async for x in response.body_iterator])
        try:
            parsed = json.loads(body)  # pyright: ignore[reportAny]
            if "detail" not in parsed or not isinstance(parsed["detail"], str):
                raise ValueError()
            error_message = cast(str, parsed["detail"])
        except json.JSONDecodeError, ValueError:
            error_message = f"An error occurred while processing your request. status={response.status_code}"

        return await raise_toast(request, ToastException(error_message, type="error"))
    return response
