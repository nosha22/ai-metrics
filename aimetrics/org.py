"""Sincroniza equipas, pessoas, identidades, licenças e repositórios a partir do YAML."""
from __future__ import annotations

import datetime as dt
import logging

from . import db

log = logging.getLogger(__name__)

SOURCES = ("github", "git_email", "claude", "cursor", "claude_api_key")


def _as_list(value) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else [str(v) for v in value]


def _date(value) -> dt.date | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def sync(conn, org: dict) -> dict:
    team_ids = {}
    for team in org["teams"]:
        row = conn.execute(
            "INSERT INTO teams (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (team["name"],),
        ).fetchone()
        team_ids[team["name"]] = row["id"]

    for repo in org["repositories"]:
        rid = db.repo_id(conn, repo["name"])
        conn.execute("UPDATE repositories SET team_id = %s WHERE id = %s", (team_ids.get(repo.get("team")), rid))

    people = 0
    for p in org["people"]:
        idents = p.get("identities") or {}
        # Procura a pessoa por qualquer identidade já ligada, para não duplicar ao mudar o nome
        person_id = None
        for source in SOURCES:
            for value in _as_list(idents.get(source)):
                person_id = person_id or db.person_for(conn, source, value)
        if person_id is None:
            row = conn.execute("SELECT id FROM people WHERE name = %s", (p["name"],)).fetchone()
            person_id = row["id"] if row else None
        values = (p["name"], team_ids.get(p.get("team")), _date(p.get("joined_on")), _date(p.get("left_on")), bool(p.get("bot", False)))
        if person_id is None:
            person_id = conn.execute(
                "INSERT INTO people (name, team_id, joined_on, left_on, is_bot) VALUES (%s,%s,%s,%s,%s) RETURNING id", values
            ).fetchone()["id"]
        else:
            conn.execute(
                "UPDATE people SET name=%s, team_id=%s, joined_on=%s, left_on=%s, is_bot=%s WHERE id=%s", (*values, person_id)
            )
        for source in SOURCES:
            for value in _as_list(idents.get(source)):
                conn.execute(
                    "INSERT INTO identities (person_id, source, value) VALUES (%s,%s,%s) "
                    "ON CONFLICT (source, value) DO UPDATE SET person_id = EXCLUDED.person_id",
                    (person_id, source, db.normalize(source, value)),
                )
        for lic in p.get("licenses") or []:
            conn.execute(
                "INSERT INTO ai_licenses (person_id, tool, valid_from, valid_to) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (person_id, tool, valid_from) DO UPDATE SET valid_to = EXCLUDED.valid_to",
                (person_id, lic["tool"], _date(lic["from"]), _date(lic.get("to"))),
            )
        people += 1

    # PRs cujo autor só agora foi ligado a uma pessoa
    conn.execute(
        """UPDATE pull_requests pr SET initiated_by = i.person_id
           FROM identities i WHERE i.id = pr.author_identity AND pr.initiated_by IS NULL AND i.person_id IS NOT NULL"""
    )
    conn.commit()
    log.info("org: %d equipas, %d pessoas, %d repos", len(team_ids), people, len(org["repositories"]))
    return {"teams": len(team_ids), "people": people, "repositories": len(org["repositories"])}


def unlinked_identities(conn) -> list[dict]:
    return conn.execute(
        "SELECT source, value, first_seen FROM identities WHERE person_id IS NULL ORDER BY source, value"
    ).fetchall()
