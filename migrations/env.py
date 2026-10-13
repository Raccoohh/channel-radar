from alembic import context

from app import models  # noqa: F401
from app.config import Settings
from app.db import Base, make_database

engine, _ = make_database(Settings().database_url.get_secret_value())
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
