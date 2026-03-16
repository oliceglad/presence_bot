from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import DATABASE_URL

ENGINE_KWARGS = {
    "echo": False,
    "pool_pre_ping": True,
    "pool_recycle": 1800,
}


def make_engine():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set in environment or .env file")
    return create_async_engine(DATABASE_URL, **ENGINE_KWARGS)

engine = make_engine()
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass
