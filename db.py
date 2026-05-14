"""
db.py — Databashantering för ECHR HUDOC MCP-servern.

Schema: echr
Tabeller:
  avgorande_cache  — cachad metadata per avgörande (nyckel: itemid)
  fulltext_cache   — cachad fulltext per avgörande (nyckel: itemid)
  sync_status      — tillståndsbaserad synk-checkpoint
"""

from __future__ import annotations

import os
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anslutning
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "")


def hamta_anslutning():
    """Returnerar en psycopg2-anslutning till PostgreSQL."""
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


# ---------------------------------------------------------------------------
# Schemainitiering
# ---------------------------------------------------------------------------

_DDL = """
CREATE SCHEMA IF NOT EXISTS echr;

CREATE TABLE IF NOT EXISTS echr.avgorande_cache (
    itemid          TEXT PRIMARY KEY,
    appno           TEXT,
    domsdatum       DATE,
    publiceringsdatum DATE,
    svarandestat    TEXT,
    ecli            TEXT,
    samling         TEXT,
    importance      INTEGER,
    artikel         TEXT,
    slutsats        TEXT,
    sprak           TEXT,
    typbeskrivning  TEXT,
    hamtad_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    uppdaterad_ts   TIMESTAMPTZ
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
    nyckel          TEXT PRIMARY KEY,
    varde           TEXT NOT NULL,
    uppdaterad_ts   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def initiera_schema():
    """Skapar schema och tabeller om de inte redan finns.

    Databasfel vid uppstart loggas som varning men stoppar inte servern.
    """
    if not DATABASE_URL:
        log.warning("DATABASE_URL är inte satt — databasen används inte")
        return

    try:
        conn = hamta_anslutning()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(_DDL)
        cur.close()
        conn.close()
        log.info("Databasschemat echr är klart")
    except Exception as e:
        log.warning("Kunde inte initiera databasen: %s", e)


# ---------------------------------------------------------------------------
# Avgörande-cache
# ---------------------------------------------------------------------------

def spara_avgorande(rad: dict) -> None:
    """Sparar eller uppdaterar ett avgörandes metadata i cachen."""
    if not DATABASE_URL:
        return
    sql = """
        INSERT INTO echr.avgorande_cache
            (itemid, appno, domsdatum, publiceringsdatum, svarandestat,
             ecli, samling, importance, artikel, slutsats, sprak,
             typbeskrivning, hamtad_ts, uppdaterad_ts)
        VALUES
            (%(itemid)s, %(appno)s, %(domsatum)s, %(publiceringsdatum)s,
             %(svarandestat)s, %(ecli)s, %(samling)s, %(importance)s,
             %(artikel)s, %(slutsats)s, %(sprak)s, %(typbeskrivning)s,
             NOW(), NOW())
        ON CONFLICT (itemid) DO UPDATE SET
            appno            = EXCLUDED.appno,
            domsdatum        = EXCLUDED.domsdatum,
            publiceringsdatum = EXCLUDED.publiceringsdatum,
            svarandestat     = EXCLUDED.svarandestat,
            ecli             = EXCLUDED.ecli,
            samling          = EXCLUDED.samling,
            importance       = EXCLUDED.importance,
            artikel          = EXCLUDED.artikel,
            slutsats         = EXCLUDED.slutsats,
            sprak            = EXCLUDED.sprak,
            typbeskrivning   = EXCLUDED.typbeskrivning,
            uppdaterad_ts    = NOW()
    """
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(sql, rad)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("Kunde inte spara avgörande %s: %s", rad.get("itemid"), e)


def hamta_avgorande(itemid: str) -> Optional[dict]:
    """Hämtar ett avgörandes metadata från cachen."""
    if not DATABASE_URL:
        return None
    sql = "SELECT * FROM echr.avgorande_cache WHERE itemid = %s"
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(sql, (itemid,))
        kolumner = [d[0] for d in cur.description]
        rad = cur.fetchone()
        cur.close()
        conn.close()
        if rad:
            return dict(zip(kolumner, rad))
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
    """Söker i den lokala cachen."""
    if not DATABASE_URL:
        return []

    villkor = []
    params: list = []

    if svarandestat:
        villkor.append("svarandestat = %s")
        params.append(svarandestat.upper())
    if importance is not None:
        villkor.append("importance = %s")
        params.append(importance)
    if artikel:
        villkor.append("artikel ILIKE %s")
        params.append(f"%{artikel}%")
    if ar_fran:
        villkor.append("EXTRACT(YEAR FROM domsatum) >= %s")
        params.append(ar_fran)
    if ar_till:
        villkor.append("EXTRACT(YEAR FROM domsatum) <= %s")
        params.append(ar_till)
    if samling:
        villkor.append("samling ILIKE %s")
        params.append(f"%{samling}%")

    where = ("WHERE " + " AND ".join(villkor)) if villkor else ""
    sql = f"""
        SELECT * FROM echr.avgorande_cache
        {where}
        ORDER BY domsatum DESC NULLS LAST
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(sql, params)
        kolumner = [d[0] for d in cur.description]
        rader = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(zip(kolumner, r)) for r in rader]
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
    sql = "SELECT fulltext_text FROM echr.fulltext_cache WHERE itemid = %s"
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(sql, (itemid,))
        rad = cur.fetchone()
        cur.close()
        conn.close()
        return rad[0] if rad else None
    except Exception as e:
        log.warning("Kunde inte läsa fulltext-cache för %s: %s", itemid, e)
        return None


def spara_fulltext(itemid: str, text: str) -> None:
    """Sparar fulltext i cachen."""
    if not DATABASE_URL:
        return
    sql = """
        INSERT INTO echr.fulltext_cache (itemid, fulltext_text, hamtad_ts)
        VALUES (%s, %s, NOW())
        ON CONFLICT (itemid) DO UPDATE SET
            fulltext_text = EXCLUDED.fulltext_text,
            hamtad_ts     = NOW()
    """
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(sql, (itemid, text))
        conn.commit()
        cur.close()
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
    sql = "SELECT varde FROM echr.sync_status WHERE nyckel = %s"
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(sql, (nyckel,))
        rad = cur.fetchone()
        cur.close()
        conn.close()
        return rad[0] if rad else None
    except Exception as e:
        log.warning("Kunde inte läsa sync_status[%s]: %s", nyckel, e)
        return None


def spara_sync_varde(nyckel: str, varde: str) -> None:
    """Sparar ett värde i sync_status."""
    if not DATABASE_URL:
        return
    sql = """
        INSERT INTO echr.sync_status (nyckel, varde, uppdaterad_ts)
        VALUES (%s, %s, NOW())
        ON CONFLICT (nyckel) DO UPDATE SET
            varde        = EXCLUDED.varde,
            uppdaterad_ts = NOW()
    """
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(sql, (nyckel, varde))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("Kunde inte spara sync_status[%s]: %s", nyckel, e)
