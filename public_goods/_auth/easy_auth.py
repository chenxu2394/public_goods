from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from .._auth.usernames import _normalize_email


_MICROSOFT_IDENTITY_PROVIDERS = {"aad", "microsoft", "microsoftaccount"}
_SUBJECT_CLAIMS = (
    "oid",
    "sub",
    "http://schemas.microsoft.com/identity/claims/objectidentifier",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier",
)
_EMAIL_CLAIMS = (
    "email",
    "preferred_username",
    "unique_name",
    "emails",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
)


@dataclass(frozen=True)
class EasyAuthIdentity:
    provider: str
    subject: str
    email: str


def _decode_client_principal(encoded: str) -> Optional[dict[str, object]]:
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.b64decode(padded).decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _claim_values(payload: dict[str, object]) -> dict[str, str]:
    values: dict[str, str] = {}
    claims = payload.get("claims")
    if not isinstance(claims, list):
        return values
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_type = claim.get("typ")
        claim_value = claim.get("val")
        if isinstance(claim_type, str) and isinstance(claim_value, str):
            values.setdefault(claim_type, claim_value)
    return values


def _first_value(values: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = values.get(name, "").strip()
        if value:
            return value
    return ""


def get_easy_auth_identity(request: Request) -> Optional[EasyAuthIdentity]:
    encoded = request.headers.get("x-ms-client-principal", "")
    payload = _decode_client_principal(encoded) if encoded else {}
    if payload is None:
        return None

    claims = _claim_values(payload)
    provider = (
        request.headers.get("x-ms-client-principal-idp")
        or str(payload.get("auth_typ") or "")
    ).strip().casefold()
    if provider not in _MICROSOFT_IDENTITY_PROVIDERS:
        return None

    subject = (
        request.headers.get("x-ms-client-principal-id")
        or _first_value(claims, _SUBJECT_CLAIMS)
    ).strip()
    name_claim_type = str(payload.get("name_typ") or "").strip()
    email_claim_names = _EMAIL_CLAIMS + ((name_claim_type,) if name_claim_type else ())
    email = (
        request.headers.get("x-ms-client-principal-name")
        or _first_value(claims, email_claim_names)
    ).strip()
    if not subject or not email or "@" not in email:
        return None

    return EasyAuthIdentity(
        provider="aad",
        subject=subject,
        email=_normalize_email(email),
    )
