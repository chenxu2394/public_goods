from __future__ import annotations

from fastapi import HTTPException


def _normalize_username(username: str) -> str:
    return username.strip().casefold()


def _validate_username(username: str) -> str:
    value = username.strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if len(value) < 3 or len(value) > 32:
        raise HTTPException(400, "Username must be 3-32 characters.")
    if any(ch not in allowed for ch in value):
        raise HTTPException(400, "Username may contain only letters, numbers, '.', '_' or '-'.")
    return value

