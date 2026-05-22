"""
db.py — Databashantering för ECHR HUDOC MCP-servern.

Välj backend via DATABASE_URL i .env:
  PostgreSQL  postgresql://anvandare:losenord@localhost:5432/databas
              Rikt FTS-stöd via GIN-index och TSVECTOR (förberett, ej aktivt ännu).
  SQLite      sqlite:///echr_cache.db
              Enkel uppstart utan extern databas.
              Sökning sker med LIKE; FTS5 är ej implementerat (framtida förbättring).

Schema: echr  (PostgreSQL — namnrymd isolerad från övriga arbetsströmmar)
Tabeller:
  avgorande_cache  — cachad metadata per avgörande (nyckel: itemid)
  fulltext_cache   — cachad fulltext per avgörande (nyckel: itemid)
  sync_status      — tillståndsbaserad synk-checkpoint
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent.resolve()

DATABASE_URL = os.getenv("DATABASE_URL", "")


# ---------------------------------------------------------------------------
# Backend-hjälpare
# ---------------------------------------------------------------------------

def _ar_postgres() -> bool:
    """Returnerar True om DATABASE_URL pekar på PostgreSQL."""
    return DATABASE_URL.startswith("postgresql")


def _hamta_db():
    """Öppnar databasanslutning — PostgreSQL eller SQLite beroende på DATABASE_URL."""
    if _ar_postgres():
        import psycopg2
        import psycopg2.extras
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        import sqlite3
        db_fil = DATABASE_URL.replace("sqlite:///", "") or "echr_cache.db"
        if not os.path.isabs(db_fil):
            db_fil = str(_SCRIPT_DIR / db_fil)
        conn = sqlite3.connect(db_fil)
        conn.row_factory = sqlite3.Row
        return conn


@contextlib.contextmanager
def _cursor(conn):
    """Kontexthanterare för databascursor (PostgreSQL och SQLite)."""
    if _ar_postgres():
        with conn.cursor() as cur:
            yield cur
    else:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()


def _prefix(tabell: str) -> str:
    """Returnerar fullt kvalificerat tabellnamn (med schema-prefix för PostgreSQL)."""
    return f"echr.{tabell}" if _ar_postgres() else tabell


def _ph() -> str:
    """Returnerar rätt platshållare för parametrar (%s för PG, ? för SQLite)."""
    return "%s" if _ar_postgres() else "?"


def _now() -> str:
    """Returnerar SQL-uttryck för aktuell tid i rätt dialekt."""
    return "NOW()" if _ar_postgres() else "datetime('now')"


# ---------------------------------------------------------------------------
# Schemainitiering
# ---------------------------------------------------------------------------

_DDL_POSTGRES = """
CREATE SCHEMA IF NOT EXISTS echr;

CREATE TABLE IF NOT EXISTS echr.avgorande_cache (
    itemid            TEXT PRIMARY KEY,
    appno             TEXT,
    domsdatum         DATE,
    publiceringsdatum DATE,
    svarandestat      TEXT,
    ecli              TEXT,
    samling           TEXT,
    importance        INTEGER,
    artikel           TEXT,
    slutsats          TEXT,
    sprak             TEXT,
    typbeskrivning    TEXT,
    hamtad_ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    uppdaterad_ts     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS echr_avgorande_svarandestat_idx
    ON echr.avgorande_cache (svarandestat);

CREATE INDEX IF NOT EXISTS echr_avgorande_importance_idx
    ON echr.avgorande_cache (importance);

CREATE INDEX IF NOT EXISTS echr_avgorande_domsdatum_idx
    ON echr.avgorande_cache (domsdatum);

CREATE INDEX IF NOT EXISTS echr_avgorande_artikel_idx
    ON echr.avgorande_cache (artikel);

CREATE TABLE IF NOT EXISTS echr.fulltext_cache (
    itemid          TEXT PRIMARY KEY
                    REFERENCES echr.avgorande_cache(itemid) ON DELETE CASCADE,
    fulltext_text   TEXT NOT NULL,
    fulltext_tsv    TSVECTOR
                    GENERATED ALWAYS AS (
                        to_tsvector('english', coalesce(fulltext_text, ''))
                    ) STORED,
    hamtad_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS echr_fulltext_fts_gin
    ON echr.fulltext_cache USING GIN (fulltext_tsv);

CREATE TABLE IF NOT EXISTS echr.sync_status (
    nyckel        TEXT PRIMARY KEY,
    varde         TEXT NOT NULL,
    uppdaterad_ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS avgorande_cache (
    itemid            TEXT PRIMARY KEY,
    appno             TEXT,
    domsdatum         TEXT,
    publiceringsdatum TEXT,
    svarandestat      TEXT,
    ecli              TEXT,
    samling           TEXT,
    importance        INTEGER,
    artikel           TEXT,
    slutsats          TEXT,
    sprak             TEXT,
    typbeskrivning    TEXT,
    hamtad_ts         TEXT NOT NULL DEFAULT (datetime('now')),
    uppdaterad_ts     TEXT
);

CREATE INDEX IF NOT EXISTS echr_avgorande_svarandestat_idx
    ON avgorande_cache (svarandestat);

CREATE INDEX IF NOT EXISTS echr_avgorande_importance_idx
    ON avgorande_cache (importance);

CREATE INDEX IF NOT EXISTS echr_avgorande_domsdatum_idx
    ON avgorande_cache (domsdatum);

CREATE INDEX IF NOT EXISTS echr_avgorande_artikel_idx
    ON avgorande_cache (artikel);

CREATE TABLE IF NOT EXISTS fulltext_cache (
    itemid        TEXT PRIMARY KEY,
    fulltext_text TEXT NOT NULL,
    hamtad_ts     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_status (
    nyckel        TEXT PRIMARY KEY,
    varde         TEXT NOT NULL,
    uppdaterad_ts TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def initiera_schema():
    """Skapar schema och tabeller om de inte redan finns.

    Väljer rätt DDL beroende på DATABASE_URL (PostgreSQL eller SQLite).
    Databasfel vid uppstart loggas som varning men stoppar inte servern.

    Migrationer: lägg framtida ALTER TABLE-satser i migrationsblocket nedan.
    Baseline-DDL (_DDL_POSTGRES/_DDL_SQLITE) ska inte ändras efter publicering —
    alla schemaändringar går via migrationsblocket.
    """
    if not DATABASE_URL:
        log.warning("DATABASE_URL är inte satt — databasen används inte")
        return

    try:
        conn = _hamta_db()
        if _ar_postgres():
            conn.autocommit = True
            with _cursor(conn) as cur:
                cur.execute(_DDL_POSTGRES)
                # --- Migrationer (lägg nya ALTER TABLE-satser här) ---
                # (inga migrationer ännu)
                # --- Slut migrationer ---
            conn.close()
        else:
            with _cursor(conn) as cur:
                for sats in _DDL_SQLITE.strip().split(";"):
                    sats = sats.strip()
                    if sats:
                        cur.execute(sats)
                # --- Migrationer (lägg nya ALTER TABLE-satser här) ---
                # (inga migrationer ännu)
                # --- Slut migrationer ---
            conn.commit()
            conn.close()
        log.info("Databasschemat echr är klart (%s)", "PostgreSQL" if _ar_postgres() else "SQLite")
    except Exception as e:
        log.warning("Kunde inte initiera databasen: %s", e)


# ---------------------------------------------------------------------------
# Avgörande-cache
# ---------------------------------------------------------------------------

def spara_avgorande(rad: dict) -> None:
    """Sparar eller uppdaterar ett avgörandes metadata i cachen."""
    if not DATABASE_URL:
        return

    ph = _ph()

    try:
        conn = _hamta_db()
        if _ar_postgres():
            sql = f"""
                INSERT INTO echr.avgorande_cache
                    (itemid, appno, domsdatum, publiceringsdatum, svarandestat,
                     ecli, samling, importance, artikel, slutsats, sprak,
                     typbeskrivning, hamtad_ts, uppdaterad_ts)
                VALUES
                    (%(itemid)s, %(appno)s, %(domsdatum)s, %(publiceringsdatum)s,
                     %(svarandestat)s, %(ecli)s, %(samling)s, %(importance)s,
                     %(artikel)s, %(slutsats)s, %(sprak)s, %(typbeskrivning)s,
                     NOW(), NOW())
                ON CONFLICT (itemid) DO UPDATE SET
                    appno             = EXCLUDED.appno,
                    domsdatum         = EXCLUDED.domsdatum,
                    publiceringsdatum = EXCLUDED.publiceringsdatum,
                    svarandestat      = EXCLUDED.svarandestat,
                    ecli              = EXCLUDED.ecli,
                    samling           = EXCLUDED.samling,
                    importance        = EXCLUDED.importance,
                    artikel           = EXCLUDED.artikel,
                    slutsats          = EXCLUDED.slutsats,
                    sprak             = EXCLUDED.sprak,
                    typbeskrivning    = EXCLUDED.typbeskrivning,
                    uppdaterad_ts     = NOW()
            """
            with _cursor(conn) as cur:
                cur.execute(sql, rad)
            conn.commit()
        else:
            sql = f"""
                INSERT INTO avgorande_cache
                    (itemid, appno, domsdatum, publiceringsdatum, svarandestat,
                     ecli, samling, importance, artikel, slutsats, sprak,
                     typbeskrivning, hamtad_ts, uppdaterad_ts)
                VALUES
                    ({ph}, {ph}, {ph}, {ph}, {ph},
                     {ph}, {ph}, {ph}, {ph}, {ph}, {ph},
                     {ph}, datetime('now'), datetime('now'))
                ON CONFLICT (itemid) DO UPDATE SET
                    appno             = excluded.appno,
                    domsdatum         = excluded.domsdatum,
                    publiceringsdatum = excluded.publiceringsdatum,
                    svarandestat      = excluded.svarandestat,
                    ecli              = excluded.ecli,
                    samling           = excluded.samling,
                    importance        = excluded.importance,
                    artikel           = excluded.artikel,
                    slutsats          = excluded.slutsats,
                    sprak             = excluded.sprak,
                    typbeskrivning    = excluded.typbeskrivning,
                    uppdaterad_ts     = datetime('now')
            """
            with _cursor(conn) as cur:
                cur.execute(sql, (
                    rad.get("itemid"), rad.get("appno"),
                    rad.get("domsdatum"), rad.get("publiceringsdatum"),
                    rad.get("svarandestat"), rad.get("ecli"),
                    rad.get("samling"), rad.get("importance"),
                    rad.get("artikel"), rad.get("slutsats"),
                    rad.get("sprak"), rad.get("typbeskrivning"),
                ))
            conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Kunde inte spara avgörande %s: %s", rad.get("itemid"), e)


def hamta_avgorande(itemid: str) -> Optional[dict]:
    """Hämtar ett avgörandes metadata från cachen."""
    if not DATABASE_URL:
        return None

    tabell = _prefix("avgorande_cache")
    ph = _ph()

    try:
        conn = _hamta_db()
        with _cursor(conn) as cur:
            cur.execute(f"SELECT * FROM {tabell} WHERE itemid = {ph}", (itemid,))
            rad = cur.fetchone()
        conn.close()
        return dict(rad) if rad else None
    except Exception as e:
        log.warning("Kunde inte hämta avgörande %s: %s", itemid, e)
        return None


def sok_avgoranden(
    svarandestat: Optional[str] = None,
    importance: Optional[int] = None,
    artikel: Optional[str] = None,
    ar_fran: Optional[int] = None,
    ar_till: Optional[int] = None,
    samling: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Söker i den lokala cachen.

    PostgreSQL: fullt stöd för ILIKE och datumextrahering.
    SQLite:     LIKE (case-insensitiv för ASCII), strftime för år.
    """
    if not DATABASE_URL:
        return []

    tabell = _prefix("avgorande_cache")
    ph = _ph()
    pg = _ar_postgres()

    villkor: list[str] = []
    params: list = []

    if svarandestat:
        villkor.append(f"svarandestat = {ph}")
        params.append(svarandestat.upper())
    if importance is not None:
        villkor.append(f"importance = {ph}")
        params.append(importance)
    if artikel:
        villkor.append(f"artikel {'ILIKE' if pg else 'LIKE'} {ph}")
        params.append(f"%{artikel}%")
    if ar_fran:
        if pg:
            villkor.append(f"EXTRACT(YEAR FROM domsdatum) >= {ph}")
        else:
            villkor.append(f"CAST(strftime('%Y', domsdatum) AS INTEGER) >= {ph}")
        params.append(ar_fran)
    if ar_till:
        if pg:
            villkor.append(f"EXTRACT(YEAR FROM domsdatum) <= {ph}")
        else:
            villkor.append(f"CAST(strftime('%Y', domsdatum) AS INTEGER) <= {ph}")
        params.append(ar_till)
    if samling:
        villkor.append(f"samling {'ILIKE' if pg else 'LIKE'} {ph}")
        params.append(f"%{samling}%")

    where = ("WHERE " + " AND ".join(villkor)) if villkor else ""
    nulls_last = "NULLS LAST" if pg else ""
    sql = f"""
        SELECT * FROM {tabell}
        {where}
        ORDER BY domsdatum DESC {nulls_last}
        LIMIT {ph} OFFSET {ph}
    """.strip()
    params.extend([limit, offset])

    try:
        conn = _hamta_db()
        with _cursor(conn) as cur:
            cur.execute(sql, params)
            rader = cur.fetchall()
        conn.close()
        return [dict(r) for r in rader]
    except Exception as e:
        log.warning("Databasfel vid sökning: %s", e)
        return []


# ---------------------------------------------------------------------------
# Fulltext-cache
# ---------------------------------------------------------------------------

def hamta_fulltext_fran_cache(itemid: str) -> Optional[str]:
    """Returnerar cachad fulltext eller None om den inte finns."""
    if not DATABASE_URL:
        return None

    tabell = _prefix("fulltext_cache")
    ph = _ph()

    try:
        conn = _hamta_db()
        with _cursor(conn) as cur:
            cur.execute(f"SELECT fulltext_text FROM {tabell} WHERE itemid = {ph}", (itemid,))
            rad = cur.fetchone()
        conn.close()
        if rad:
            return rad["fulltext_text"] if isinstance(rad, dict) else rad[0]
        return None
    except Exception as e:
        log.warning("Kunde inte läsa fulltext-cache för %s: %s", itemid, e)
        return None


def spara_fulltext(itemid: str, text: str) -> None:
    """Sparar fulltext i cachen."""
    if not DATABASE_URL:
        return

    tabell = _prefix("fulltext_cache")
    ph = _ph()
    nu = _now()

    try:
        conn = _hamta_db()
        sql = f"""
            INSERT INTO {tabell} (itemid, fulltext_text, hamtad_ts)
            VALUES ({ph}, {ph}, {nu})
            ON CONFLICT (itemid) DO UPDATE SET
                fulltext_text = excluded.fulltext_text,
                hamtad_ts     = {nu}
        """
        with _cursor(conn) as cur:
            cur.execute(sql, (itemid, text))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Kunde inte spara fulltext för %s: %s", itemid, e)


# ---------------------------------------------------------------------------
# Sync-status
# ---------------------------------------------------------------------------

def hamta_sync_varde(nyckel: str) -> Optional[str]:
    """Läser ett värde från sync_status."""
    if not DATABASE_URL:
        return None

    tabell = _prefix("sync_status")
    ph = _ph()

    try:
        conn = _hamta_db()
        with _cursor(conn) as cur:
            cur.execute(f"SELECT varde FROM {tabell} WHERE nyckel = {ph}", (nyckel,))
            rad = cur.fetchone()
        conn.close()
        if rad:
            return rad["varde"] if isinstance(rad, dict) else rad[0]
        return None
    except Exception as e:
        log.warning("Kunde inte läsa sync_status[%s]: %s", nyckel, e)
        return None


def spara_sync_varde(nyckel: str, varde: str) -> None:
    """Sparar ett värde i sync_status."""
    if not DATABASE_URL:
        return

    tabell = _prefix("sync_status")
    ph = _ph()
    nu = _now()

    try:
        conn = _hamta_db()
        sql = f"""
            INSERT INTO {tabell} (nyckel, varde, uppdaterad_ts)
            VALUES ({ph}, {ph}, {nu})
            ON CONFLICT (nyckel) DO UPDATE SET
                varde         = excluded.varde,
                uppdaterad_ts = {nu}
        """
        with _cursor(conn) as cur:
            cur.execute(sql, (nyckel, varde))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Kunde inte spara sync_status[%s]: %s", nyckel, e)
