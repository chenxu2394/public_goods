from __future__ import annotations

from fastapi import HTTPException


def _normalize_username(username: str) -> str:
    return username.strip().casefold()


def _normalize_email(email: str) -> str:
    return email.strip().casefold()


def _validate_username(username: str) -> str:
    value = username.strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if len(value) < 3 or len(value) > 32:
        raise HTTPException(400, "Username must be 3-32 characters.")
    if any(ch not in allowed for ch in value):
        raise HTTPException(400, "Username may contain only letters, numbers, '.', '_' or '-'.")
    return value


def _validate_email(email: str) -> str:
    value = email.strip()
    if len(value) > 254 or value.count("@") != 1:
        raise HTTPException(400, "Enter a valid Microsoft account email address.")
    local, domain = value.rsplit("@", 1)
    if not local or not domain or "." not in domain or any(ch.isspace() for ch in value):
        raise HTTPException(400, "Enter a valid Microsoft account email address.")
    return value
