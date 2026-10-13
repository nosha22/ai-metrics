"""Acesso à base de dados: ligação, schema e resolução de identidades."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DB_DIR = Path(__file__).resolve().parent.parent / "db"


@contextmanager
def connect(url: str):
    with psycopg.connect(url, row_factory=dict_row, autocommit=False) as conn:
        yield conn


def apply_schema(conn) -> None:
    for name in ("schema.sql", "views.sql"):
        conn.execute((DB_DIR / name).read_text())
    conn.commit()


def normalize(source: str, value: str) -> str:
    value = value.strip()
    # Emails e logins GitHub não distinguem maiúsculas
    return value.lower()


def get_or_create_identity(conn, source: str, value: str | None) -> int | None:
    """Devolve o id da identidade, criando-a por ligar (person_id NULL) se for nova.

    Se a identidade nova for um email que já existe noutra fonte ligada a uma pessoa,
    liga-a automaticamente à mesma pessoa (ex.: email Claude igual ao email do git).
    """
    if not value:
        return None
    value = normalize(source, value)
    row = conn.execute(
        "SELECT id FROM identities WHERE source = %s AND value = %s", (source, value)
    ).fetchone()
    if row:
        return row["id"]
    person_id = None
    if "@" in value:
        match = conn.execute(
            "SELECT person_id FROM identities WHERE value = %s AND person_id IS NOT NULL LIMIT 1",
            (value,),
        ).fetchone()
        person_id = match["person_id"] if match else None
    row = conn.execute(
        "INSERT INTO identities (person_id, source, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (source, value) DO UPDATE SET value = EXCLUDED.value RETURNING id",
        (person_id, source, value),
    ).fetchone()
    return row["id"]


def person_for(conn, source: str, value: str | None) -> int | None:
    if not value:
        return None
    row = conn.execute(
        "SELECT person_id FROM identities WHERE source = %s AND value = %s",
        (source, normalize(source, value)),
    ).fetchone()
    return row["person_id"] if row else None


def repo_id(conn, full_name: str) -> int:
    row = conn.execute(
        "INSERT INTO repositories (full_name) VALUES (%s) "
        "ON CONFLICT (full_name) DO UPDATE SET full_name = EXCLUDED.full_name RETURNING id",
        (full_name.lower(),),
    ).fetchone()
    return row["id"]


def get_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, value),
    )
