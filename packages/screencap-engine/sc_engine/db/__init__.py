"""Package for interacting with the recording database.

Adapted for per-capture databases.
"""

import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import MetaData

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class BaseModel:
    """The base model for database tables."""

    __abstract__ = True

    def __repr__(self) -> str:
        """Return a string representation of the model object."""
        params = ", ".join(
            f"{k}={v!r}"
            for k, v in {
                c.name: getattr(self, c.name)
                for c in self.__table__.columns
            }.items()
            if v is not None
        )
        return f"{self.__class__.__name__}({params})"


def get_base() -> sa.engine:
    """Create and return the base model.

    Returns:
        The base model object.
    """
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Base = declarative_base(
        cls=BaseModel,
        metadata=metadata,
    )
    return Base


Base = get_base()


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """Set SQLite PRAGMAs on every new connection for write performance."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-64000")
    cursor.close()


def get_engine(db_url: str, echo: bool = False) -> sa.engine:
    """Create and return a database engine.

    Args:
        db_url: SQLAlchemy database URL (e.g. sqlite:///path/to/db).
        echo: Whether to echo SQL statements.
    """
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        echo=echo,
    )
    sa.event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def get_session_maker(engine: sa.engine) -> sessionmaker:
    """Create a session maker bound to the given engine."""
    return sessionmaker(bind=engine)


def create_db(db_path: str, echo: bool = False) -> tuple:
    """Create a new database at the given path, returning (engine, Session).

    Creates all tables defined in the models.

    Args:
        db_path: Path to the SQLite database file.
        echo: Whether to echo SQL statements.

    Returns:
        tuple of (engine, Session class).
    """
    db_url = f"sqlite:///{db_path}"
    engine = get_engine(db_url, echo=echo)

    # Import models to ensure they are registered with Base
    from sc_engine.db import models  # noqa: F401

    Base.metadata.create_all(engine)
    Session = get_session_maker(engine)
    return engine, Session


def _sa_type_to_sqlite(sa_col) -> str:
    """Map a SQLAlchemy column type to a SQLite type string for ALTER TABLE."""
    from sqlalchemy import Boolean, Integer, LargeBinary, Numeric, String, Text, JSON
    from sqlalchemy import TypeDecorator

    col_type = sa_col.type
    if isinstance(col_type, TypeDecorator):
        col_type = col_type.impl

    sa_type = type(col_type)
    mapping = {
        Integer: "INTEGER",
        String: "TEXT",
        Text: "TEXT",
        Boolean: "BOOLEAN",
        Numeric: "NUMERIC",
        LargeBinary: "BLOB",
        JSON: "TEXT",
    }
    sqlite_type = mapping.get(sa_type, "TEXT")

    if sa_col.default is not None and sa_col.default.arg is not None:
        arg = sa_col.default.arg
        if isinstance(arg, bool):
            sqlite_type += f" DEFAULT {int(arg)}"
        elif isinstance(arg, (int, float)):
            sqlite_type += f" DEFAULT {arg}"

    return sqlite_type


def _migrate_schema(db_path: str) -> None:
    """Add missing columns to existing databases.

    Compares every table that exists in the DB against the SQLAlchemy model
    definitions and issues ALTER TABLE ADD COLUMN for anything missing.
    Called before opening a session so that queries don't fail on old databases.
    """
    import sqlite3

    from sc_engine.db import models  # noqa: F401 — registers models

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    for table in Base.metadata.sorted_tables:
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table.name,),
        )
        if not cur.fetchone():
            continue

        cur.execute(f"PRAGMA table_info({table.name})")
        existing_cols = {row[1] for row in cur.fetchall()}

        for col in table.columns:
            if col.name not in existing_cols:
                sqlite_type = _sa_type_to_sqlite(col)
                cur.execute(
                    f"ALTER TABLE {table.name} ADD COLUMN {col.name} {sqlite_type}"
                )

    conn.commit()
    conn.close()


def get_session_for_path(db_path: str, echo: bool = False):
    """Create and return a new session for the given database path.

    This is used by worker processes to get their own session to the
    per-capture database.

    Args:
        db_path: Path to the SQLite database file.
        echo: Whether to echo SQL statements.

    Returns:
        A SQLAlchemy Session instance.
    """
    _migrate_schema(db_path)
    db_url = f"sqlite:///{db_path}"
    engine = get_engine(db_url, echo=echo)
    Session = get_session_maker(engine)
    return Session()
