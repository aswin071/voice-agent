"""Initialize database tables."""
import asyncio
from db import engine, Base
from api.models import *  # Import all models


async def init_db():
    """Create all database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Database tables created successfully!")


if __name__ == "__main__":
    asyncio.run(init_db())
