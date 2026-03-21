from __future__ import annotations

import base64
import io
import sqlite3
from typing import Dict

import segno
from fastapi import Request

from ..config import PUBLIC_BASE_URL
from .types import ShareLinkContext


def qr_svg_data_uri(content: str) -> str:
    qr = segno.make_qr(content, error="m")
    output = io.BytesIO()
    qr.save(output, kind="svg", scale=6, border=2, dark="#111111", light="#ffffff")
    svg = output.getvalue()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def build_share_link_context(sess: sqlite3.Row) -> ShareLinkContext:
    join_url = f"{PUBLIC_BASE_URL}/join/{sess['join_token']}"
    return {
        "join_url": join_url,
        "join_qr_data_uri": qr_svg_data_uri(join_url),
    }


def build_share_link_page_context(request: Request, sess: sqlite3.Row) -> Dict[str, object]:
    context: Dict[str, object] = {
        "request": request,
        "sess": sess,
        "share_api_url": f"{PUBLIC_BASE_URL}/api/admin/{sess['id']}/share_link",
    }
    context.update(build_share_link_context(sess))
    return context


def build_share_link_api_payload(sess: sqlite3.Row) -> Dict[str, object]:
    payload = build_share_link_context(sess)
    return {
        "session": {
            "id": sess["id"],
            "title": sess["title"],
        },
        "join_url": payload["join_url"],
        "join_qr_data_uri": payload["join_qr_data_uri"],
    }
