from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse

from .config import APP_TITLE
from .db import init_db
from .routes import admin, display, student
from .views import templates


DEMO_IMAGE_PATH = Path(__file__).resolve().parents[1] / "docs" / "assets" / "demo.webp"


@asynccontextmanager
async def _lifespan(_: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE, lifespan=_lifespan)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(request, "home.html", {"request": request})

    @app.get("/demo.webp", include_in_schema=False, response_class=FileResponse)
    def home_demo():
        return FileResponse(DEMO_IMAGE_PATH, media_type="image/webp")

    app.include_router(admin.router)
    app.include_router(student.router)
    app.include_router(display.router)
    return app


app = create_app()
