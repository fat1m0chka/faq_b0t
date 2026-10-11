from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, String, Boolean

engine = create_async_engine("sqlite+aiosqlite:///nextgen.db", echo=False)
AsyncSessionMaker = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase): 
    pass

class Participant(Base):
    __tablename__ = "giveaway_participants"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    full_name: Mapped[str] = mapped_column(String)
    phone: Mapped[str] = mapped_column(String, unique=True, nullable=True)
    tokens: Mapped[int] = mapped_column(default=0)
    ss_login: Mapped[str] = mapped_column(String, nullable=True)
    ss_password: Mapped[str] = mapped_column(String, nullable=True)
    ss_cid: Mapped[int] = mapped_column(BigInteger, nullable=True)

class Contest(Base):
    __tablename__ = "contests"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    channel_id: Mapped[str] = mapped_column(String, nullable=True)
    photo_id: Mapped[str] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)