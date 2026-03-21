from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .config import APP_TITLE
from .db import init_db
from .routes import admin, display, student
from .views import templates


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE)

    @app.on_event("startup")
    def _startup():
        init_db()

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse("home.html", {"request": request})

    app.include_router(admin.router)
    app.include_router(student.router)
    app.include_router(display.router)
    return app


app = create_app()
