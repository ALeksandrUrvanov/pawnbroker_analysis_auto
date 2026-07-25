"""Подключение к PostgreSQL (переменные DB_* из .env)."""
from __future__ import annotations

import os
from typing import Any


def get_db_connection() -> Any:
    import psycopg2

    host = os.getenv("DB_HOST", "localhost")
    name = os.getenv("DB_NAME", "postgres")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    port = os.getenv("DB_PORT", "5432")
    return psycopg2.connect(
        host=host,
        port=int(port),
        user=user,
        password=password,
        database=name,
        connect_timeout=10,
    )
