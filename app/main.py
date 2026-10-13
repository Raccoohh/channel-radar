import asyncio
import hmac
import logging
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.ai import AIUnavailable, DigestService, GeminiProvider
from app.analytics import period_start, summary
from app.collection import Collector
from app.config import Settings
from app.db import make_database, utcnow
from app.models import Channel, ChannelMetric, CollectionRun, Control, Digest, Post, PostMetric
from app.telegram import TelegramClient, normalize_username

ROOT = Path(__file__).parent
Days = Annotated[int, Query(ge=1, le=365)]


class ChannelInput(BaseModel):
    username: str = Field(min_length=1, max_length=120)


def create_app(settings=None, sessions=None, source=None, provider=None):
    settings = settings or Settings()
    engine = None
    if sessions is None:
        engine, sessions = make_database(settings.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=4)) as http:
            app.state.collector = Collector(
                sessions, source or TelegramClient(http), settings.collection_interval_seconds
            )
            app.state.digests = DigestService(
                sessions,
                provider
                or GeminiProvider(
                    http, settings.gemini_api_key.get_secret_value(), settings.gemini_model
                ),
                settings.gemini_model,
                settings.ai_daily_limit,
            )
            task = (
                asyncio.create_task(app.state.collector.run()) if settings.worker_enabled else None
            )
            yield
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if engine:
            engine.dispose()

    app = FastAPI(title="Channel Radar", lifespan=lifespan)
    app.state.sessions = sessions
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.filters["number"] = lambda v: f"{v:,}" if v is not None else "N/A"
    templates.env.filters["stamp"] = lambda v: (
        v.strftime("%d %b %Y · %H:%M UTC") if v else "Not yet"
    )

    @app.middleware("http")
    async def security(request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            expected = urlsplit(settings.public_url)
            if origin and origin != f"{expected.scheme}://{expected.netloc}":
                return JSONResponse({"detail": "Cross-origin request rejected."}, status_code=403)
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 4096:
                return JSONResponse({"detail": "Request too large."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    def get_channel(db, channel_id):
        channel = db.get(Channel, channel_id)
        if not channel:
            raise HTTPException(404, "Channel not found.")
        return channel

    def add_channel(value):
        try:
            username = normalize_username(value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            with sessions.begin() as db:
                existing = db.scalar(select(Channel).where(Channel.username == username))
                if existing:
                    return existing, False
                now = utcnow()
                if not db.execute(
                    update(Control)
                    .where(
                        Control.id == 1,
                        Control.next_add_at <= now,
                    )
                    .values(next_add_at=now + timedelta(seconds=3))
                ).rowcount:
                    raise HTTPException(429, "Please wait 3 seconds before adding another channel.")
                if db.scalar(select(func.count(Channel.id))) >= settings.max_channels:
                    raise HTTPException(409, "This demo has reached its channel limit.")
                channel = Channel(username=username)
                db.add(channel)
                db.flush()
            return channel, True
        except IntegrityError:
            with sessions() as db:
                existing = db.scalar(select(Channel).where(Channel.username == username))
                if existing:
                    return existing, False
            raise

    def channel_rows(db):
        return db.execute(
            select(Channel, func.count(Post.id).label("post_count"))
            .outerjoin(Post, Post.channel_id == Channel.id)
            .group_by(Channel.id)
            .order_by(Channel.created_at.desc())
        ).all()

    def render_overview(request, error=None, status_code=200):
        with sessions() as db:
            rows = channel_rows(db)
        return templates.TemplateResponse(
            request=request,
            name="overview.html",
            context={
                "rows": rows,
                "error": error,
                "max_channels": settings.max_channels,
                "total_posts": sum(row.post_count for row in rows),
                "healthy": sum(row.Channel.status == "healthy" for row in rows),
                "poll_state": [channel_version(row.Channel) for row in rows],
            },
            status_code=status_code,
        )

    @app.get("/healthz")
    def health():
        try:
            with sessions() as db:
                db.execute(text("SELECT 1"))
                if db.get(Control, 1) is None:
                    raise HTTPException(503, "Database migrations required.")
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Database unavailable.") from exc
        return {"status": "ok"}

    @app.get("/")
    def overview(request: Request):
        return render_overview(request)

    @app.post("/channels")
    async def add_form(request: Request, username: Annotated[str, Form(max_length=120)]):
        try:
            add_channel(username)
        except HTTPException as exc:
            return render_overview(request, exc.detail, exc.status_code)
        app.state.collector.wake.set()
        return RedirectResponse("/", status_code=303)

    @app.post("/api/channels")
    async def add_api(body: ChannelInput):
        channel, created = add_channel(body.username)
        app.state.collector.wake.set()
        return JSONResponse(
            {
                "id": channel.id,
                "username": channel.username,
                "status": channel.status,
                "created": created,
            },
            status_code=201 if created else 200,
        )

    @app.get("/api/channels")
    def channels_api():
        with sessions() as db:
            return [
                {
                    "id": c.id,
                    "username": c.username,
                    "status": c.status,
                    "posts": count,
                    "last_success": c.last_success_at,
                    "error": c.last_error,
                    "version": channel_version(c),
                }
                for c, count in channel_rows(db)
            ]

    def channel_version(channel):
        return ":".join(
            [
                str(channel.id),
                channel.status,
                channel.last_success_at.isoformat() if channel.last_success_at else "",
            ]
        )

    @app.get("/channels/{channel_id}")
    def channel_page(
        request: Request,
        channel_id: int,
        days: Days = 30,
        page: Annotated[int, Query(ge=1, le=10000)] = 1,
    ):
        with sessions() as db:
            channel = get_channel(db, channel_id)
            stats = summary(db, channel_id, days)
            posts = db.scalars(
                select(Post)
                .where(
                    Post.channel_id == channel_id,
                    Post.published_at >= period_start(days),
                )
                .order_by(Post.published_at.desc(), Post.id.desc())
                .offset((page - 1) * 30)
                .limit(30)
            ).all()
            digest = db.scalar(
                select(Digest)
                .where(
                    Digest.channel_id == channel_id,
                    Digest.days == days,
                    Digest.status == "ready",
                )
                .order_by(Digest.id.desc())
            )
            runs = db.scalars(
                select(CollectionRun)
                .where(CollectionRun.channel_id == channel_id)
                .order_by(CollectionRun.id.desc())
                .limit(5)
            ).all()
        return templates.TemplateResponse(
            request=request,
            name="channel.html",
            context={
                "channel": channel,
                "stats": stats,
                "posts": posts,
                "days": days,
                "digest": digest,
                "runs": runs,
                "page": page,
                "has_next": stats["posts"] > page * 30,
                "poll_state": [channel_version(channel)],
            },
        )

    @app.get("/api/channels/{channel_id}/trend")
    def channel_trend(channel_id: int, days: Days = 30):
        with sessions() as db:
            get_channel(db, channel_id)
            points = db.scalars(
                select(ChannelMetric)
                .where(
                    ChannelMetric.channel_id == channel_id,
                    ChannelMetric.captured_at >= period_start(days),
                )
                .order_by(ChannelMetric.captured_at.desc())
                .limit(1000)
            ).all()
        return [{"time": p.captured_at, "value": p.subscribers} for p in reversed(points)]

    @app.get("/posts/{post_id}")
    def post_page(request: Request, post_id: int):
        with sessions() as db:
            post = db.get(Post, post_id)
            if not post:
                raise HTTPException(404, "Post not found.")
            channel = get_channel(db, post.channel_id)
            metrics = db.scalars(
                select(PostMetric)
                .where(PostMetric.post_id == post_id)
                .order_by(PostMetric.captured_at.desc())
                .limit(100)
            ).all()
        return templates.TemplateResponse(
            request=request,
            name="post.html",
            context={
                "post": post,
                "channel": channel,
                "metrics": metrics,
            },
        )

    @app.get("/api/posts/{post_id}/trend")
    def post_trend(post_id: int):
        with sessions() as db:
            if not db.get(Post, post_id):
                raise HTTPException(404, "Post not found.")
            points = db.scalars(
                select(PostMetric)
                .where(PostMetric.post_id == post_id)
                .order_by(PostMetric.captured_at.desc())
                .limit(1000)
            ).all()
        return [{"time": p.captured_at, "value": p.views} for p in reversed(points)]

    @app.post("/api/channels/{channel_id}/digest")
    async def generate_digest(channel_id: int, days: Days = 30):
        with sessions() as db:
            get_channel(db, channel_id)
        try:
            digest = await app.state.digests.generate(channel_id, days)
            return {
                "content": digest.content,
                "created_at": digest.created_at,
                "source_count": digest.source_count,
                "model": digest.model,
            }
        except AIUnavailable as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.post("/internal/collect")
    async def scheduled_collect(request: Request):
        secret = settings.scheduler_token.get_secret_value()
        supplied = request.headers.get("authorization", "")
        if not secret or not hmac.compare_digest(supplied, f"Bearer {secret}"):
            raise HTTPException(401, "Invalid scheduler credentials.")
        # Wait for completion: a success response is not just an in-memory queue acknowledgement.
        return await app.state.collector.collect_due()

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
