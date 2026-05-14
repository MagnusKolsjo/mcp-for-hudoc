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
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import requests
import db

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent.resolve()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_SCRIPT_DIR / "logs" / "synk.log", encoding="utf-8"),
    ],
)
(_SCRIPT_DIR / "logs").mkdir(parents=True, exist_ok=True)
log = logging.getLogger(__name__)

HUDOC_SOKRESULTAT_MAX = 500          # Max poster per HUDOC-anrop (HUDOC tillåter upp till 500)
HUDOC_TIMEOUT         = 30           # Sekunder
HUDOC_PAUS_SEKUNDER   = 0.3          # Paus mellan anrop för att inte hammra API:t

# HUDOC kräver en specifik rankingModelId för att results-endpointen ska svara.
# Utan denna parameter returnerar endpointen 404.
RANKING_MODEL_ID = "4180000c-8692-45ca-ad63-74bc4163871b"

# Bas-query med XRANK-rankning — exakt det format HUDOC förväntar sig.
# Utan contentsitename:ECHR och XRANK-strukturen returnerar endpointen 404.
_HUDOC_BAS_QUERY = (
    "(((((((((((((((((((( contentsitename:ECHR "
    "AND (NOT (doctype=PR))) "
    "XRANK(cb=14) doctypebranch:GRANDCHAMBER) "
    "XRANK(cb=13) doctypebranch:DECGRANDCHAMBER) "
    "XRANK(cb=12) doctypebranch:CHAMBER) "
    "XRANK(cb=11) doctypebranch:ADMISSIBILITY) "
    "XRANK(cb=10) doctypebranch:COMMITTEE) "
    "XRANK(cb=9) doctypebranch:ADMISSIBILITYCOM) "
    "XRANK(cb=8) doctypebranch:DECCOMMISSION) "
    "XRANK(cb=7) doctypebranch:COMMUNICATEDCASES) "
    "XRANK(cb=6) doctypebranch:CLIN) "
    "XRANK(cb=5) doctypebranch:ADVISORYOPINIONS) "
    "XRANK(cb=4) doctypebranch:REPORTS) "
    "XRANK(cb=3) doctypebranch:EXECUTION) "
    "XRANK(cb=2) doctypebranch:MERITS) "
    "XRANK(cb=1) doctypebranch:SCREENINGPANEL) "
    "XRANK(cb=4) importance:1) "
    "XRANK(cb=3) importance:2) "
    "XRANK(cb=2) importance:3) "
    "XRANK(cb=1) importance:4) "
    "XRANK(cb=2) languageisocode:ENG) "
    "XRANK(cb=1) languageisocode:FRE"
)

SELECT_FALT = (
    "itemid,appno,judgementdate,kpdate,respondent,ecli,"
    "doctypebranch,importance,article,conclusion,languageisocode,typedescription"
)

# ---------------------------------------------------------------------------
# HUDOC-hjälpfunktioner
# ---------------------------------------------------------------------------

def _bygg_hudoc_session() -> requests.Session:
    """Skapar en requests-session med rätt headers för HUDOC."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _hudoc_sok(
    session: requests.Session,
    extra_filter: str,
    start: int = 0,
    antal: int = HUDOC_SOKRESULTAT_MAX,
    datum_fran: str | None = None,
) -> dict:
    """Kör en sökning mot HUDOC och returnerar råa JSON-svaret.

    Använder HUDOC:s obligatoriska XRANK-basquery med ett extra AND-filter.
    Kräver HTTP (ej HTTPS) och rankingModelId — utan dessa returneras 404.
    """
    # OBS: Lägg INTE extra parenteser runt bas-queryn — XRANK-syntaxen bryts då.
    # Filter läggs direkt till med AND i slutet av kedjan.
    # sort ska vara tom sträng (som originalet) — inte "judgementdate Descending".
    query = _HUDOC_BAS_QUERY
    if extra_filter:
        query = f"{query} AND ({extra_filter})"
    if datum_fran:
        query = f"{query} AND (kpdate>=\"{datum_fran}\")"

    url = (
        "http://hudoc.echr.coe.int/app/query/results"
        f"?query={urllib.parse.quote(query)}"
        f"&select={SELECT_FALT}"
        f"&rankingModelId={RANKING_MODEL_ID}"
        f"&sort="
        f"&start={start}&length={antal}"
    )
    svar = session.get(url, timeout=HUDOC_TIMEOUT)
    svar.raise_for_status()
    return svar.json()


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
        "domsatum":         _datum(kolumner.get("judgementdate")),
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

def synka(force_full: bool = False) -> None:
    """Kör synkroniseringen."""
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

    session = _bygg_hudoc_session()

    # Definiera de två sökkriterierna som extra AND-filter ovanpå bas-queryn
    fragor = [
        ("importance=1 (alla stater)",        "importance=1"),
        ("Case Reports / Key cases",          "doctypebranch=REPORTS"),
        ("respondent=SWE (alla nivåer)",      "respondent=SWE"),
    ]

    totalt_sparade = 0

    for beskrivning, extra_filter in fragor:
        log.info("--- %s ---", beskrivning)
        try:
            poster = _hamta_alla_sidor(
                session, extra_filter, beskrivning, datum_fran=datum_fran
            )
        except Exception as e:
            log.error("Fel vid hämtning för '%s': %s", beskrivning, e)
            continue

        for hudoc_rad in poster:
            rad = _konvertera_rad(hudoc_rad)
            if not rad["itemid"]:
                continue
            db.spara_avgorande(rad)
            totalt_sparade += 1

        log.info("Sparade %d poster för '%s'", len(poster), beskrivning)

    # Spara dagens datum som checkpoint
    idag = date.today().isoformat()
    db.spara_sync_varde("senaste_synk_datum", idag)

    log.info("=== Synk klar. Totalt sparade/uppdaterade: %d ===", totalt_sparade)


# ---------------------------------------------------------------------------
# Schemaläggning
# ---------------------------------------------------------------------------

def installera_schema(script_sokvag: str, python_sokvag: str) -> None:
    """Installerar dagligt schemalagt synk-jobb (launchd eller cron).

    Styrs av SCHEMALAGGARE i .env:
      launchd  — macOS-nativt, körs även efter viloläge (rekommenderas på Mac)
      cron     — fungerar på Linux och macOS

    Tidpunkt styrs av CRON_SCHEMA i .env (standard: 03:15 varje natt).
    Python-sökväg styrs av PYTHON_SOKVÄG i .env (standard: .venv/bin/python3
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
    python_rel = os.getenv("PYTHON_SOKVÄG", ".venv/bin/python3")
    if not os.path.isabs(python_rel):
        python_abs = str(skript_mapp / python_rel)
    else:
        python_abs = python_rel

    if schemalaggare == "launchd":
        if platform.system() != "Darwin":
            log.error("launchd är bara tillgängligt på macOS. Byt SCHEMALAGGARE=cron i .env.")
            return

        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_fil = plist_dir / "se.riksdag-ai.echr-hudoc-synk.plist"
        plist_dir.mkdir(parents=True, exist_ok=True)

        delar  = cron_schema.split()
        minut, timme = delar[0], delar[1]

        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>se.riksdag-ai.echr-hudoc-synk</string>
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
        python_sokvag = os.getenv("PYTHON_SOKVÄG", ".venv/bin/python3")
        installera_schema(__file__, python_sokvag)
    else:
        synka(force_full=args.force_full)
