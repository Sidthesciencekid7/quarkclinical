from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/pkbe")

# Per-worker connection ceiling = DB_POOL_SIZE + DB_MAX_OVERFLOW. Defaults match
# SQLAlchemy's own pre-existing pool defaults (5 + 10 = 15/worker) rather than a
# narrower value, so moving from 1 to N Uvicorn workers (Procfile/Dockerfile
# --workers) doesn't silently shrink the per-worker burst headroom each process
# already had — total connections across all workers = 15 * worker count, which
# must stay under the Postgres plan's max_connections. Env-configurable rather
# than hardcoded so a future worker-count change can re-tune this without a
# code edit.
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
engine = create_async_engine(DATABASE_URL, echo=False, pool_size=DB_POOL_SIZE, max_overflow=DB_MAX_OVERFLOW)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
