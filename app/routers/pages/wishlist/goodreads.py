import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, Security
from sqlmodel import Session, select

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.db_queries import get_wishlist_counts
from app.internal.models import GoodreadsQueuedBook, GroupEnum, ManualBookRequest
from app.util.db import get_session
from app.util.redirect import BaseUrlRedirectResponse
from app.util.templates import catalog_response

router = APIRouter(prefix="/goodreads")


@router.get("")
def goodreads_wishlist(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
):
    username = None if user.is_admin() else user.username
    books = session.exec(
        select(GoodreadsQueuedBook)
        .where(not username or GoodreadsQueuedBook.username == username)
        .order_by(GoodreadsQueuedBook.queued_at.desc())  # type: ignore[arg-type]
    ).all()
    counts = get_wishlist_counts(session, user)
    return catalog_response(
        "Wishlist.Goodreads",
        user=user,
        books=books,
        counts=counts,
    )


@router.post("/hx-search/{goodreads_book_id}")
def search_prowlarr_for_book(
    goodreads_book_id: str,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    """
    Create a ManualBookRequest from a not_found Goodreads book and redirect
    to the sources picker so the admin can choose a torrent manually.
    """
    username = user.username
    book = session.get(GoodreadsQueuedBook, (goodreads_book_id, username))
    if not book:
        # Admins can see all users' books; try any username
        book = session.exec(
            select(GoodreadsQueuedBook).where(
                GoodreadsQueuedBook.goodreads_book_id == goodreads_book_id
            )
        ).first()
    if not book:
        return Response(status_code=404)

    # Create a ManualBookRequest so the existing sources picker can handle it
    manual = ManualBookRequest(
        user_username=username,
        title=book.title,
        authors=[book.author] if book.author else [],
    )
    session.add(manual)
    session.commit()
    session.refresh(manual)

    from app.internal.env_settings import Settings
    base_url = Settings().app.base_url.rstrip("/")
    return Response(
        status_code=204,
        headers={"HX-Redirect": f"{base_url}/wishlist/sources/{manual.id}"},
    )


@router.delete("/hx-delete/{goodreads_book_id}")
def delete_goodreads_book(
    goodreads_book_id: str,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
):
    username = user.username
    book = session.get(GoodreadsQueuedBook, (goodreads_book_id, username))
    if book:
        session.delete(book)
        session.commit()

    books = session.exec(
        select(GoodreadsQueuedBook)
        .where(not username or GoodreadsQueuedBook.username == username)
        .order_by(GoodreadsQueuedBook.queued_at.desc())  # type: ignore[arg-type]
    ).all()
    counts = get_wishlist_counts(session, user)
    return catalog_response(
        "Wishlist.GoodreadsWishlist",
        user=user,
        books=books,
        counts=counts,
        update_tablist=True,
    )
