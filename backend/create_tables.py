"""One-shot table creation script — bypasses Alembic migrations entirely."""
import asyncio
from dotenv import load_dotenv
load_dotenv()

from database import engine, Base
import models  # noqa: registers all models with Base.metadata
from sqlalchemy import text


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("All tables created successfully.")

    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO users (id, org_id, email, role, hashed_password)
            VALUES (
                '00000000-0000-0000-0000-000000000001',
                'default-org',
                'system@pkbe.local',
                'generator',
                'not-a-real-hash'
            )
            ON CONFLICT (id) DO NOTHING
        """))
        print("Default system user seeded.")

    await engine.dispose()


asyncio.run(main())
