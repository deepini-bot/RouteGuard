from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from gateway.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

_kwargs: dict = {"pool_pre_ping": True}
if _is_sqlite:
    # The app uses a thread pool for DB work, and SQLite needs to allow that
    _kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(settings.database_url, **_kwargs)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        yield db


def init_db() -> None:
    from gateway.database.models import Base

    Base.metadata.create_all(engine)
