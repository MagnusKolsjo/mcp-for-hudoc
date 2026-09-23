"""
02_synka_metadata.py — Synkar ECHR-avgöranden från HUDOC till lokal PostgreSQL-databas.

Strategi:
  - Hämtar metadata (ej fulltext) för:
      (a) Alla avgöranden med importance=1       (~2 800 st, alla stater)
      (b) Alla Case Reports / Key cases          (~3 100 st, doctypebranch=REPORTS)
      (c) Alla avgöranden med respondent=SWE    (~540 st, alla importance-nivåer)
  - Tillståndsbaserat: sparar senaste synk-datum i sync_status och hämtar
    bara nya/uppdaterade poster vid körningar efter den första.
  - Fulltext hämtas inte här — det sker on-demand av MCP-servern.

Användning:
  python3 02_synka_metadata.py [--force-full] [--installera-schema]

  --force-full         Synkar om allt från 1959, ignorerar tidigare checkpoint.
  --installera-schema  Installerar dagligt schemalagt jobb (launchd eller cron).
                       Styrs av SCHEMALAGGARE i .env (standard: launchd på macOS).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import requests
import db
import hudoc_query
from hudoc_query import _HUDOC_BAS_QUERY

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent.resolve()

# Skapa log-mappen INNAN logging.basicConfig — FileHandler kräver att mappen finns.
(_SCRIPT_DIR / "logs").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_SCRIPT_DIR / "logs" / "synk.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

HUDOC_SOKRESULTAT_MAX = 500          # Max poster per HUDOC-anrop (HUDOC tillåter upp till 500)
HUDOC_TIMEOUT         = 30           # Sekunder
HUDOC_PAUS_SEKUNDER   = 0.3          # Paus mellan anrop för att inte hammra API:t

# ---------------------------------------------------------------------------
# HUDOC-hjälpfunktioner
# ---------------------------------------------------------------------------

def _hudoc_sok(
    session: requests.Session,
    extra_filter: str,
    start: int = 0,
    antal: int = HUDOC_SOKRESULTAT_MAX,
    datum_fran: str | None = None,
) -> dict:
    """Kör en sökning mot HUDOC och returnerar råa JSON-svaret.

    Använder HUDOC:s obligatoriska XRANK-basquery med ett extra AND-filter.
    """
    # OBS: Lägg INTE extra parenteser runt bas-queryn — XRANK-syntaxen bryts då.
    # Filter läggs direkt till med AND i slutet av kedjan.
    query = _HUDOC_BAS_QUERY
    if extra_filter:
        query = f"{query} AND ({extra_filter})"
    if datum_fran:
        query = f"{query} AND (kpdate>=\"{datum_fran}\")"

    return hudoc_query.sok(session, query, start=start, antal=antal, timeout=HUDOC_TIMEOUT)


def _hamta_alla_sidor(
    session: requests.Session,
    extra_filter: str,
    beskrivning: str,
    datum_fran: str | None = None,
) -> list[dict]:
    """Hämtar alla sidor för ett HUDOC-filter och returnerar en platt lista med poster."""

    # Första anropet för att ta reda på totalt antal
    forsta = _hudoc_sok(session, extra_filter, start=0, antal=1, datum_fran=datum_fran)
    totalt = forsta.get("resultcount", 0)
    if totalt == 0:
        log.info("Inga poster att synka för: %s", beskrivning)
        return []

    log.info("Hämtar %d poster för: %s", totalt, beskrivning)

    poster: list[dict] = []
    start = 0
    while start < totalt:
        batch = _hudoc_sok(
            session, extra_filter,
            start=start, antal=HUDOC_SOKRESULTAT_MAX,
            datum_fran=datum_fran,
        )
        rader = batch.get("results", [])
        if not rader:
            break
        poster.extend(rader)
        start += len(rader)
        log.info("  Hämtat %d / %d", min(start, totalt), totalt)
        time.sleep(HUDOC_PAUS_SEKUNDER)

    return poster


# ---------------------------------------------------------------------------
# Konvertering av HUDOC-rad till DB-format
# ---------------------------------------------------------------------------

def _konvertera_rad(hudoc_rad: dict) -> dict:
    """Konverterar ett HUDOC-resultatobjekt till DB-format."""
    kolumner = hudoc_rad.get("columns", {})

    def _datum(s: str | None) -> str | None:
        """Rensar HUDOC-datumformat till ISO-datum (YYYY-MM-DD)."""
        if not s:
            return None
        # HUDOC returnerar ibland "01/01/1970 00:00:00" eller "1970-01-01"
        for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s.strip(), fmt).date().isoformat()
            except ValueError:
                continue
        return s.strip() or None

    def _importance(s: str | None) -> int | None:
        try:
            return int(s) if s else None
        except (ValueError, TypeError):
            return None

    return {
        "itemid":           kolumner.get("itemid", ""),
        "appno":            kolumner.get("appno", ""),
        "domsdatum":        _datum(kolumner.get("judgementdate")),
        "publiceringsdatum": _datum(kolumner.get("kpdate")),
        "svarandestat":     kolumner.get("respondent", ""),
        "ecli":             kolumner.get("ecli", ""),
        "samling":          kolumner.get("doctypebranch", ""),
        "importance":       _importance(kolumner.get("importance")),
        "artikel":          kolumner.get("article", ""),
        "slutsats":         kolumner.get("conclusion", ""),
        "sprak":            kolumner.get("languageisocode", ""),
        "typbeskrivning":   kolumner.get("typedescription", ""),
    }


# ---------------------------------------------------------------------------
# Huvudlogik
# ---------------------------------------------------------------------------

def synka(force_full: bool = False) -> bool:
    """Kör synkroniseringen. Returnerar True om allt hämtades och sparades.

    Checkpointen (senaste_synk_datum) flyttas bara fram när varje filter
    hämtats och varje rad sparats. Annars skulle nästa inkrementella körning
    börja efter luckan, och avgöranden publicerade under ett avbrott skulle
    aldrig hämtas.
    """
    log.info("=== Startar ECHR-metadatasynk ===")

    db.initiera_schema()

    # Hämta senaste synkdatum (eller None om första körning)
    if force_full:
        datum_fran = None
        log.info("--force-full: synkar allt från 1959")
    else:
        datum_fran = db.hamta_sync_varde("senaste_synk_datum")
        if datum_fran:
            log.info("Inkrementell synk från %s", datum_fran)
        else:
            log.info("Första synken — hämtar alla poster")

    session = hudoc_query.skapa_session()

    # Sökkriterierna läggs som extra AND-filter ovanpå bas-queryn
    fragor = [
        ("importance=1 (alla stater)",        "importance=1"),
        ("Case Reports / Key cases",          "doctypebranch=REPORTS"),
        ("respondent=SWE (alla nivåer)",      "respondent=SWE"),
    ]

    totalt_sparade = 0
    totalt_misslyckade = 0
    hamtningsfel = False

    for beskrivning, extra_filter in fragor:
        log.info("--- %s ---", beskrivning)
        try:
            poster = _hamta_alla_sidor(
                session, extra_filter, beskrivning, datum_fran=datum_fran
            )
        except hudoc_query.BotskyddFel as e:
            # Övriga filter skulle mötas av samma kontroll; fler anrop
            # förlänger bara blockeringen.
            log.error("Synken avbryts: %s", e)
            hamtningsfel = True
            break
        except Exception as e:
            log.error("Fel vid hämtning för '%s': %s", beskrivning, e)
            hamtningsfel = True
            continue

        sparade = 0
        for hudoc_rad in poster:
            rad = _konvertera_rad(hudoc_rad)
            if not rad["itemid"]:
                continue
            if db.spara_avgorande(rad):
                sparade += 1
            else:
                totalt_misslyckade += 1
        totalt_sparade += sparade

        log.info("Sparade %d av %d poster för '%s'", sparade, len(poster), beskrivning)

    lyckad = not hamtningsfel and totalt_misslyckade == 0
    if lyckad:
        idag = date.today().isoformat()
        if not db.spara_sync_varde("senaste_synk_datum", idag):
            lyckad = False
    else:
        log.error(
            "Checkpointen flyttas inte fram (hämtningsfel: %s, misslyckade rader: %d). "
            "Nästa körning börjar om från %s.",
            "ja" if hamtningsfel else "nej", totalt_misslyckade, datum_fran or "början",
        )

    log.info(
        "=== Synk klar. Sparade/uppdaterade: %d, misslyckade: %d ===",
        totalt_sparade, totalt_misslyckade,
    )
    return lyckad


# ---------------------------------------------------------------------------
# Schemaläggning
# ---------------------------------------------------------------------------

def installera_schema(script_sokvag: str, python_sokvag: str) -> None:
    """Installerar dagligt schemalagt synk-jobb (launchd eller cron).

    Styrs av SCHEMALAGGARE i .env:
      launchd  — macOS-nativt, körs även efter viloläge (rekommenderas på Mac)
      cron     — fungerar på Linux och macOS

    Tidpunkt styrs av CRON_SCHEMA i .env (standard: 03:15 varje natt).
    Python-sökväg styrs av PYTHON_SOKVAG i .env (standard: .venv/bin/python3
    relativt skriptmappen).
    """
    import platform
    import subprocess
    from pathlib import Path

    schemalaggare = os.getenv("SCHEMALAGGARE", "launchd").lower()
    cron_schema   = os.getenv("CRON_SCHEMA", "15 3 * * *")
    script_abs    = str(Path(script_sokvag).resolve())
    skript_mapp   = Path(script_sokvag).parent.resolve()

    # Bygg absolut Python-sökväg
    python_rel = os.getenv("PYTHON_SOKVAG", ".venv/bin/python3")
    if not os.path.isabs(python_rel):
        python_abs = str(skript_mapp / python_rel)
    else:
        python_abs = python_rel

    if schemalaggare == "launchd":
        if platform.system() != "Darwin":
            log.error("launchd är bara tillgängligt på macOS. Byt SCHEMALAGGARE=cron i .env.")
            return

        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_fil = plist_dir / "se.magnuskolsjo.mcp-echr-synk.plist"
        plist_dir.mkdir(parents=True, exist_ok=True)

        delar  = cron_schema.split()
        minut, timme = delar[0], delar[1]

        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>se.magnuskolsjo.mcp-echr-synk</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_abs}</string>
        <string>{script_abs}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{timme}</integer>
        <key>Minute</key>
        <integer>{minut}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{Path.home()}/Library/Logs/echr-hudoc-synk.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/Library/Logs/echr-hudoc-synk-fel.log</string>
</dict>
</plist>"""

        with open(plist_fil, "w") as fh:
            fh.write(plist)

        subprocess.run(["launchctl", "load", str(plist_fil)], check=True)
        log.info("launchd-jobb installerat: %s", plist_fil)
        log.info("Kör dagligen kl. %s:%s. Loggar: ~/Library/Logs/", timme, minut)

    else:
        # cron — fungerar på Linux och macOS
        rad = f"{cron_schema} {python_abs} {script_abs}\n"
        befintlig = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        ).stdout

        if script_abs in befintlig:
            log.info("Cron-jobb finns redan. Ingen ändring gjord.")
            return

        ny_crontab = befintlig + rad
        proc = subprocess.run(["crontab", "-"], input=ny_crontab, text=True)
        if proc.returncode == 0:
            log.info("Cron-jobb tillagt: %s", rad.strip())
        else:
            log.error("Kunde inte uppdatera crontab.")


# ---------------------------------------------------------------------------
# Startpunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Synkar ECHR-metadata från HUDOC till lokal databas."
    )
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="Synka allt från 1959, ignorera tidigare checkpoint.",
    )
    parser.add_argument(
        "--installera-schema",
        action="store_true",
        help="Installera dagligt schemalagt jobb (launchd eller cron, se .env).",
    )
    args = parser.parse_args()

    if args.installera_schema:
        python_sokvag = os.getenv("PYTHON_SOKVAG", ".venv/bin/python3")
        installera_schema(__file__, python_sokvag)
    else:
        sys.exit(0 if synka(force_full=args.force_full) else 1)
