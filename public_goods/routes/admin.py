from __future__ import annotations

from fastapi import APIRouter

from ._admin import auth_home, demo, rounds, sessions, sharing, teachers


router = APIRouter()
router.include_router(auth_home.router)
router.include_router(teachers.router)
router.include_router(sessions.router)
router.include_router(rounds.router)
router.include_router(demo.router)
router.include_router(sharing.router)


__all__ = ["router"]
