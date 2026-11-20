"""Pool de conexiones a Postgres (backend)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            config.DATABASE_URL,
            min_size=1,
            max_size=10,
            kwargs={
                "row_factory": dict_row,
                "options": "-c search_path=public,extensions",
                # Supabase pooler (transaction mode) no soporta prepared statements
                "prepare_threshold": None,
            },
            open=True,
        )
    return _pool


@contextmanager
def conexion() -> Iterator[psycopg.Connection]:
    """Conexión con transacción: commit al salir bien, rollback si hay error."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def cerrar() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
