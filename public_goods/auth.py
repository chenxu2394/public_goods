from __future__ import annotations

from ._auth.passwords import (
    _generate_password_salt,
    _hash_password_record,
    _hash_password_with_salt,
    _legacy_hash_password,
    generate_temp_password,
    verify_user_password,
)
from ._auth.easy_auth import EasyAuthIdentity, get_easy_auth_identity
from ._auth.requests import _auth_gate, _management_gate, get_current_user
from ._auth.tokens import (
    _auth_configuration_error,
    _b64url,
    _b64url_decode,
    _is_auth_configured,
    _must_configure_auth,
    _sign,
    make_auth_token,
    verify_auth_token,
)
from ._auth.usernames import (
    _normalize_email,
    _normalize_username,
    _validate_email,
    _validate_username,
)


__all__ = [
    "_auth_configuration_error",
    "_auth_gate",
    "_b64url",
    "_b64url_decode",
    "_generate_password_salt",
    "_hash_password_record",
    "_hash_password_with_salt",
    "_is_auth_configured",
    "_legacy_hash_password",
    "_management_gate",
    "_must_configure_auth",
    "_normalize_email",
    "_normalize_username",
    "_sign",
    "_validate_email",
    "_validate_username",
    "EasyAuthIdentity",
    "generate_temp_password",
    "get_easy_auth_identity",
    "get_current_user",
    "make_auth_token",
    "verify_auth_token",
    "verify_user_password",
]
