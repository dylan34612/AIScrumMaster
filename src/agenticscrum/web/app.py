"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import traceback
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from agenticscrum.chat_markdown import render_chat_markdown
from agenticscrum.config import PROJECT_ROOT, Settings, load_settings
from agenticscrum.db import init_db
from agenticscrum.scheduler import build_scheduler
from agenticscrum.web.routes import router

templates = Jinja2Templates(directory=PROJECT_ROOT / "src" / "agenticscrum" / "web" / "templates")


def _relative_time(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return value
    if not isinstance(value, datetime):
        return str(value)
    when = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - when.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        mins = seconds // 60
        return f"{mins}m ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    if days < 7:
        return f"{days}d ago"
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _chat_markdown(text: object, ado_org: object = None, ado_project: object = None) -> str:
    return render_chat_markdown(
        str(text or ""),
        ado_org=str(ado_org) if ado_org else None,
        ado_project=str(ado_project) if ado_project else None,
    )


templates.env.filters["relative_time"] = _relative_time
templates.env.filters["chat_markdown"] = _chat_markdown


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the Agentic Scrum web app."""

    config = settings or load_settings()
    init_db(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler = build_scheduler(config)
        scheduler.start()
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Agentic Scrum", lifespan=lifespan)
    app.state.settings = config
    app.state.templates = templates
    app.include_router(router)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return request.app.state.templates.TemplateResponse(
            request,
            "error.html",
            {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": tb,
                "when": datetime.now(timezone.utc).isoformat(),
                "path": request.url.path,
                "method": request.method,
            },
            status_code=500,
        )

    return app


app = create_app()
