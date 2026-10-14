# Component: Spindle Architecture
# File: database.py
# Description: Database engine factory for the Spindle orchestration platform.

"""Database engine and session factory.

This module provides factory functions only — it does not read from the
environment and does not create any engine at import time.  Keeping
construction in functions lets the composition root (lifespan in main.py)
build the engine from a validated URL obtained from Settings, so a
misconfigured deploy fails fast rather than silently using a wrong database.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


def create_db_engine(url: str) -> Engine:
    """Return a new SQLAlchemy engine for *url*.

    ``pool_pre_ping=True`` is kept from the original configuration so the
    pool discards stale connections before handing them to callers.  No
    other pool arguments are added here; tuning belongs in a later ops task.
    The caller (lifespan) is responsible for calling ``engine.dispose()`` on
    shutdown.
    """
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a sessionmaker bound to *engine*.

    No additional keyword arguments are added beyond what the original
    ``sessionmaker(bind=engine)`` call used, which had none.  The original
    was a bare ``sessionmaker(bind=engine)``, so no autoflush / autocommit /
    expire_on_commit overrides are applied here.
    """
    return sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass
