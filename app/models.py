from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, utcnow


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    subscribers: Mapped[int | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="pending")
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_collection_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_message_id: Mapped[int] = mapped_column(default=0)
    backfill_before: Mapped[int | None] = mapped_column()
    catchup_target: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("channel_id", "message_id", name="uq_post_channel_message"),
        Index("ix_posts_channel_published", "channel_id", "published_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    message_id: Mapped[int] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime())
    url: Mapped[str] = mapped_column(String(200))
    views: Mapped[int | None] = mapped_column()
    reactions: Mapped[int | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PostMetric(Base):
    __tablename__ = "post_metric_snapshots"
    __table_args__ = (Index("ix_post_metrics_time", "post_id", "captured_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    views: Mapped[int | None] = mapped_column()
    reactions: Mapped[int | None] = mapped_column()
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ChannelMetric(Base):
    __tablename__ = "channel_metric_snapshots"
    __table_args__ = (Index("ix_channel_metrics_time", "channel_id", "captured_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    subscribers: Mapped[int | None] = mapped_column()
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    token: Mapped[str] = mapped_column(String(36), unique=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), default="collecting")
    collected_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(String(500))


class Digest(Base):
    __tablename__ = "ai_digests"
    __table_args__ = (Index("ix_digest_cache", "channel_id", "fingerprint", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    days: Mapped[int] = mapped_column()
    model: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(30), default="gemini")
    status: Mapped[str] = mapped_column(String(20), default="generating")
    content: Mapped[str] = mapped_column(Text, default="")
    source_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class Control(Base):
    """One database row serializes public resource and provider budget admission."""

    __tablename__ = "controls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_add_at: Mapped[datetime] = mapped_column(UTCDateTime())
    next_ai_at: Mapped[datetime] = mapped_column(UTCDateTime())
