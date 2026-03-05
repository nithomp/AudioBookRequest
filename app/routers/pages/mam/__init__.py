from fastapi import APIRouter

from . import freeleech

router = APIRouter()

router.include_router(freeleech.router)
