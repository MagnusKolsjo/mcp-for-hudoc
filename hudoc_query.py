"""
hudoc_query.py — Gemensam åtkomst till HUDOC för MCP-servern och synkskriptet.

Samlar det som annars riskerar att glida isär mellan mcp_server.py och
02_synka_metadata.py: adresserna, bas-queryn, fältlistan, HTTP-sessionen och
själva sökanropet.

Miljövariabler (läses när sessionen skapas, efter load_dotenv i anroparen):
    HUDOC_USER_AGENT  User-Agent i alla anrop (standard: projektets egen)

Ingångspunkter:
    skapa_session() -> requests.Session
    sok(session, query, start, antal, timeout) -> dict
    SOK_URL, FULLTEXT_URL, _HUDOC_BAS_QUERY, SELECT_FALT, RANKING_MODEL_ID
"""

from __future__ import annotations

import os
import urllib.parse

import requests

HUDOC_BAS_URL = "https://hudoc.echr.coe.int"
SOK_URL       = f"{HUDOC_BAS_URL}/app/query/results"
FULLTEXT_URL  = f"{HUDOC_BAS_URL}/app/conversion/docx/html/body"

# Projektets egen User-Agent. Den identifierar anropen som automatiserade och
# pekar på repot, så att källan kan se vem som frågar och höra av sig.
STANDARD_USER_AGENT = "mcp-for-hudoc/1.0 (+https://github.com/MagnusKolsjo/mcp-for-hudoc)"

# HUDOC:s results-endpoint kräver rankingModelId; utan den svarar den 404.
RANKING_MODEL_ID = "4180000c-8692-45ca-ad63-74bc4163871b"

# Bas-query med obligatorisk XRANK-struktur som HUDOC:s results-endpoint kräver.
# Utan contentsitename:ECHR och XRANK-strukturen returneras 404.
# OBS: Lägg INTE extra parenteser runt bas-queryn — XRANK-syntaxen bryts då.
# Extra filter läggs till med AND i slutet av kedjan (se respektive modul).
_HUDOC_BAS_QUERY = (
    "(((((((((((((((((((( contentsitename:ECHR "
    "AND (NOT (doctype=PR OR doctype=HFCOMOLD OR doctype=HECOMOLD))) "
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

# Metadatafält att begära i varje HUDOC-sökresultat.
SELECT_FALT = (
    "itemid,appno,judgementdate,kpdate,respondent,ecli,"
    "doctypebranch,importance,article,conclusion,languageisocode,typedescription"
)


def skapa_session() -> requests.Session:
    """Skapar en requests-session med HUDOC-headers.

    Sessionen delas mellan MCP-serverns arbetstrådar. Headers sätts därför
    en gång här och ändras aldrig efteråt.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": os.getenv("HUDOC_USER_AGENT", "").strip() or STANDARD_USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def sok(
    session: requests.Session,
    query: str,
    start: int = 0,
    antal: int = 20,
    timeout: float = 30,
) -> dict:
    """Kör en sökning mot HUDOC och returnerar det råa JSON-svaret.

    `query` är den fullständiga query-strängen, alltså bas-queryn med
    eventuella AND-filter. `sort` lämnas tom; HUDOC rangordnar då efter
    XRANK-strukturen.
    """
    url = (
        f"{SOK_URL}"
        f"?query={urllib.parse.quote(query)}"
        f"&select={SELECT_FALT}"
        f"&rankingModelId={RANKING_MODEL_ID}"
        f"&sort="
        f"&start={start}&length={antal}"
    )
    svar = session.get(url, timeout=timeout)
    svar.raise_for_status()
    return svar.json()
