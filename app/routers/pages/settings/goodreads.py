from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Response, Security
from sqlmodel import Session, select

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.models import GoodreadsQueuedBook, GoodreadsUserConfig
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter(prefix="/goodreads")


def _get_or_create_config(db: Session, username: str) -> GoodreadsUserConfig:
    cfg = db.get(GoodreadsUserConfig, username)
    if not cfg:
        cfg = GoodreadsUserConfig(username=username)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


@router.get("")
def read_goodreads_settings(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
):
    cfg = _get_or_create_config(session, user.username)
    queued_books = session.exec(
        select(GoodreadsQueuedBook)
        .where(GoodreadsQueuedBook.username == user.username)
        .order_by(GoodreadsQueuedBook.queued_at.desc())  # type: ignore[arg-type]
    ).all()

    return catalog_response(
        "Settings.Goodreads",
        user=user,
        page="goodreads",
        rss_url=cfg.rss_url or "",
        last_polled=cfg.last_polled,
        auto_download=cfg.auto_download,
        queued_books=queued_books,
    )


@router.put("/hx-rss-url", status_code=204)
def update_rss_url(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    rss_url: Annotated[str, Form()],
):
    cfg = _get_or_create_config(session, user.username)
    cfg.rss_url = rss_url.strip()
    session.add(cfg)
    session.commit()
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/hx-auto-download", status_code=204)
def update_auto_download(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    auto_download: Annotated[bool, Form()] = False,
):
    cfg = _get_or_create_config(session, user.username)
    cfg.auto_download = auto_download
    session.add(cfg)
    session.commit()
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/hx-poll-now")
async def poll_now(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    background_tasks: BackgroundTasks,
):
    """Trigger an immediate poll for the current user's shelf."""
    from app.internal.goodreads.poller import poll_user_shelf

    cfg = _get_or_create_config(session, user.username)
    if not cfg.rss_url:
        return Response(status_code=204)

    rss_url = cfg.rss_url
    auto_download = cfg.auto_download
    username = user.username

    async def _run():
        await poll_user_shelf(username, rss_url, auto_download)

    background_tasks.add_task(_run)
    return Response(status_code=204, headers={"HX-Refresh": "true"})
