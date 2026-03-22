from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...auth import (
    _must_configure_auth,
    get_current_user,
    make_auth_token,
    verify_user_password,
)
from ...config import ADMIN_COOKIE_SECURE, AUTH_COOKIE_NAME, AUTH_TOKEN_TTL_SECONDS, LEGACY_ADMIN_COOKIE_NAME
from ...db import db
from ..._sessions import _rehash_legacy_user_password_conn, get_user_by_id, get_user_by_username, set_user_password
from ..helpers import require_authenticated_user
from .common import admin_home_response, login_page_response


router = APIRouter()


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/admin", status_code=303)
    return login_page_response(request)


@router.post("/admin/login")
def admin_login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    _must_configure_auth()
    user = get_user_by_username(username)
    if not user:
        return login_page_response(request, status_code=401, error="Incorrect username or password.")
    if user["disabled_at"] is not None:
        return login_page_response(request, status_code=403, error="This account is disabled.")

    password_ok, needs_rehash = verify_user_password(user, password)
    if not password_ok:
        return login_page_response(request, status_code=401, error="Incorrect username or password.")

    if needs_rehash:
        conn = db()
        try:
            _rehash_legacy_user_password_conn(conn, str(user["id"]), password)
            conn.commit()
        finally:
            conn.close()
        user = get_user_by_id(str(user["id"]))

    token = make_auth_token(user)
    redirect_url = "/admin?pw_change_required=1" if int(user["must_change_password"]) == 1 else "/admin"
    resp = RedirectResponse(url=redirect_url, status_code=303)
    resp.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        secure=ADMIN_COOKIE_SECURE,
        samesite="lax",
        max_age=AUTH_TOKEN_TTL_SECONDS,
        path="/",
    )
    resp.delete_cookie(LEGACY_ADMIN_COOKIE_NAME, path="/")
    return resp


@router.post("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    resp.delete_cookie(LEGACY_ADMIN_COOKIE_NAME, path="/")
    return resp


@router.post("/admin/change_password")
def admin_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user, gate = require_authenticated_user(request)
    if gate:
        return gate

    def render_error(error: str):
        return admin_home_response(request, user, status_code=400, pw_error=error)

    password_ok, _ = verify_user_password(user, current_password)
    if not password_ok:
        return render_error("Current password is incorrect.")
    if not new_password:
        return render_error("New password must not be empty.")
    if new_password != confirm_password:
        return render_error("New passwords do not match.")
    set_user_password(str(user["id"]), new_password, must_change_password=False)
    resp = RedirectResponse(url="/admin/login?pw_changed=1", status_code=303)
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    resp.delete_cookie(LEGACY_ADMIN_COOKIE_NAME, path="/")
    return resp


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    user, gate = require_authenticated_user(request)
    if gate:
        return gate
    return admin_home_response(request, user)
