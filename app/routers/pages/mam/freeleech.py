from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Security
from fastapi.responses import Response
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.mam.config import mam_freeleech_config
from app.internal.mam.freeleech import (
    MAM_BASE_URL,
    _MAM_PROXY,
    fetch_mam_freeleech,
    get_mam_session_id,
)
from app.internal.models import GroupEnum
from app.util.connection import USER_AGENT, get_connection
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter(prefix="/freeleech")


@router.get("")
async def get_freeleech_page(
    db_session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.trusted))],
    force_refresh: bool = False,
):
    """
    The MaM Freeleech browse page.

    Accessible to all Trusted and Admin users.
    """
    ttl = mam_freeleech_config.get_ttl_seconds(db_session)

    result = await fetch_mam_freeleech(
        db_session=db_session,
        client_session=client_session,
        ttl_seconds=ttl,
        force_refresh=force_refresh,
    )

    return catalog_response(
        "Freeleech.Index",
        user=user,
        result=result,
        ttl_minutes=ttl // 60,
    )


@router.get("/cover/{torrent_id}")
async def get_cover_image(
    torrent_id: int,
    db_session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.trusted))],
):
    """
    Proxy MaM cover art through the VPN so the session cookie is applied
    and the image loads for users regardless of their IP.
    """
    mam_id = get_mam_session_id(db_session)
    cover_url = f"https://i.myanonamouse.net/t/{torrent_id}.jpg"
    try:
        async with client_session.get(
            cover_url,
            cookies={"mam_id": mam_id} if mam_id else {},
            headers={"User-Agent": USER_AGENT, "Referer": MAM_BASE_URL},
            proxy=_MAM_PROXY,
        ) as resp:
            if resp.status == 200:
                content = await resp.read()
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                return Response(content=content, media_type=content_type)
    except Exception:
        pass
    return Response(status_code=404)
