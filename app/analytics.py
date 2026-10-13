from datetime import timedelta

from sqlalchemy import func, select

from app.db import utcnow
from app.models import Post


def period_start(days: int):
    return utcnow() - timedelta(days=days)


def summary(db, channel_id: int, days: int):
    count, views, reactions, known_views, known_reactions = db.execute(
        select(
            func.count(Post.id),
            func.sum(Post.views),
            func.sum(Post.reactions),
            func.count(Post.views),
            func.count(Post.reactions),
        ).where(Post.channel_id == channel_id, Post.published_at >= period_start(days))
    ).one()
    return {
        "posts": count,
        "views": views,
        "reactions": reactions,
        "known_views": known_views,
        "known_reactions": known_reactions,
        "average_views": round(views / known_views) if known_views else None,
    }
