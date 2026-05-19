from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Response, Security
from sqlmodel import Session, select

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.goodreads.config import goodreads_config
from app.internal.models import GroupEnum, GoodreadsQueuedBook
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter(prefix="/goodreads")


@router.get("")
def read_goodreads_settings(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    rss_url = goodreads_config.get_rss_url(session) or ""
    last_polled = goodreads_config.get_last_polled(session)
    auto_download = goodreads_config.get_auto_download(session)
    queued_books = session.exec(
        select(GoodreadsQueuedBook).order_by(GoodreadsQueuedBook.queued_at.desc())  # type: ignore[arg-type]
    ).all()

    return catalog_response(
        "Settings.Goodreads",
        user=admin_user,
        page="goodreads",
        rss_url=rss_url,
        last_polled=last_polled,
        auto_download=auto_download,
        queued_books=queued_books,
    )


@router.put("/hx-rss-url", status_code=204)
def update_rss_url(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    rss_url: Annotated[str, Form()],
):
    _ = admin_user
    goodreads_config.set_rss_url(session, rss_url.strip())
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/hx-auto-download", status_code=204)
def update_auto_download(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    auto_download: Annotated[bool, Form()] = False,
):
    _ = admin_user
    goodreads_config.set_auto_download(session, auto_download)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/hx-poll-now")
async def poll_now(
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    background_tasks: BackgroundTasks,
):
    """Trigger an immediate shelf poll. Runs in a background task so the response is instant."""
    from app.internal.goodreads.poller import poll_goodreads_shelf
    from app.util.db import get_session as _get_session

    _ = admin_user

    async def _run():
        with next(_get_session()) as db:
            await poll_goodreads_shelf(db)

    background_tasks.add_task(_run)
    return Response(status_code=204, headers={"HX-Refresh": "true"})
