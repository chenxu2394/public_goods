from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ...auth import _validate_email, _validate_username, generate_temp_password
from ...config import AUTH_MODE, AUTH_MODE_EASY_AUTH, USER_ROLE_TEACHER
from ..._sessions import (
    _get_teacher_or_404,
    create_microsoft_user,
    create_user,
    remove_teacher_and_reassign_sessions,
    set_user_disabled,
    set_user_password,
)
from ..helpers import require_admin_user
from .common import admin_home_response


router = APIRouter()


@router.post("/admin/teachers")
def admin_create_teacher(
    request: Request,
    username: str | None = Form(None),
    email: str | None = Form(None),
):
    user, gate = require_admin_user(request)
    if gate:
        return gate

    try:
        if AUTH_MODE == AUTH_MODE_EASY_AUTH:
            teacher_email = _validate_email(email or "")
            create_microsoft_user(teacher_email, USER_ROLE_TEACHER)
            return admin_home_response(
                request,
                user,
                teacher_success=f"Approved teacher Microsoft account '{teacher_email}'.",
            )

        if username is None:
            raise HTTPException(400, "Username is required.")
        username = _validate_username(username)
        temp_password = generate_temp_password()
        create_user(
            username,
            USER_ROLE_TEACHER,
            temp_password,
            must_change_password=True,
        )
    except HTTPException as exc:
        return admin_home_response(request, user, status_code=exc.status_code, teacher_error=exc.detail)
    except sqlite3.IntegrityError:
        message = (
            "That Microsoft account is already approved."
            if AUTH_MODE == AUTH_MODE_EASY_AUTH
            else "That username already exists."
        )
        return admin_home_response(request, user, status_code=400, teacher_error=message)

    return admin_home_response(
        request,
        user,
        teacher_success=f"Created teacher '{username}'.",
        teacher_temp_password=temp_password,
        teacher_temp_password_username=username,
    )


@router.post("/admin/teachers/{user_id}/disable")
def admin_disable_teacher(request: Request, user_id: str):
    user, gate = require_admin_user(request)
    if gate:
        return gate

    teacher = _get_teacher_or_404(user_id)
    set_user_disabled(str(teacher["id"]), True)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/teachers/{user_id}/enable")
def admin_enable_teacher(request: Request, user_id: str):
    user, gate = require_admin_user(request)
    if gate:
        return gate

    teacher = _get_teacher_or_404(user_id)
    set_user_disabled(str(teacher["id"]), False)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/teachers/{user_id}/delete")
def admin_remove_teacher(request: Request, user_id: str):
    user, gate = require_admin_user(request)
    if gate:
        return gate

    remove_teacher_and_reassign_sessions(user_id)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/teachers/{user_id}/reset_password")
def admin_reset_teacher_password(request: Request, user_id: str):
    user, gate = require_admin_user(request)
    if gate:
        return gate
    if AUTH_MODE == AUTH_MODE_EASY_AUTH:
        raise HTTPException(404, "Not found")

    teacher = _get_teacher_or_404(user_id)
    temp_password = generate_temp_password()
    set_user_password(str(teacher["id"]), temp_password, must_change_password=True)
    return admin_home_response(
        request,
        user,
        teacher_success=f"Reset password for '{teacher['username']}'.",
        teacher_temp_password=temp_password,
        teacher_temp_password_username=str(teacher["username"]),
    )
