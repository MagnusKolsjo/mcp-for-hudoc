"""
hudoc_query.py — Gemensam åtkomst till HUDOC för MCP-servern och synkskriptet.

Samlar det som annars riskerar att glida isär mellan mcp_server.py och
02_synka_metadata.py: adresserna, bas-queryn, fältlistan, HTTP-sessionen och
själva sökanropet.

Botskydd: HUDOC ligger bakom Cloudflare och kan svara med en botkontroll
("Just a moment…") i stället för data. Det är källans uttryckliga val att
stoppa automatiserade anrop, och modulen försöker aldrig ta sig förbi det.
Ett sådant svar blir BotskyddFel, och därefter avstår modulen från nya anrop
under en paus, så att upprepade försök inte förlänger blockeringen.

Miljövariabler (läses efter load_dotenv i anroparen):
    HUDOC_USER_AGENT            User-Agent i alla anrop (standard: projektets egen)
    HUDOC_BOTSKYDD_PAUS_MINUTER Paus efter en botkontroll (standard: 10)

Ingångspunkter:
    skapa_session() -> requests.Session
    hamta(session, url, **kwargs) -> requests.Response
    sok(session, query, start, antal, timeout) -> dict
    BotskyddFel
    SOK_URL, FULLTEXT_URL, _HUDOC_BAS_QUERY, SELECT_FALT, RANKING_MODEL_ID
"""

from __future__ import annotations

import os
import threading
import time
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


class BotskyddFel(Exception):
    """HUDOC svarade med Cloudflares botkontroll i stället för data.

    Skiljer blockeringen från vanliga HTTP-fel, så att verktygen kan förklara
    läget i stället för att rapportera ett rått 403. Meddelandet är skrivet
    för slutanvändaren och kan visas som det är.
    """


# Tecken på Cloudflares utmaningssida när cf-mitigated-headern saknas.
_UTMANINGSMARKORER = ("just a moment", "challenge-platform", "cf-chl", "cf_chl_opt")

_BOTSKYDD_PAUS_SEKUNDER = max(0.0, float(os.getenv("HUDOC_BOTSKYDD_PAUS_MINUTER", "10")) * 60)

# Tidpunkt (time.time) då botkontrollen senast sågs, eller None. Delas mellan
# arbetstrådar och skyddas därför av ett lås.
_blockerad_sedan: float | None = None
_blockerad_las = threading.Lock()


def _ar_botskydd(svar: requests.Response) -> bool:
    """Avgör om ett svar är Cloudflares botkontroll."""
    if svar.status_code not in (403, 503):
        return False
    if svar.headers.get("cf-mitigated"):
        return True
    if "html" in svar.headers.get("content-type", "").lower():
        borjan = svar.text[:5000].lower()
        return any(m in borjan for m in _UTMANINGSMARKORER)
    return False


def _meddelande(status: int | None, sedan: float) -> str:
    """Bygger felmeddelandet till användaren."""
    klockslag = time.strftime("%H:%M", time.localtime(sedan))
    orsak = (
        f"HUDOC svarade med Cloudflares botkontroll (HTTP {status}) i stället för data"
        if status is not None
        else f"HUDOC svarade med Cloudflares botkontroll kl. {klockslag}, "
             "och servern avstår från nya anrop en stund för att inte förlänga blockeringen"
    )
    return (
        "HUDOC (Europadomstolens databas) blockerar just nu automatiserade anrop. "
        f"{orsak}. Det är inte ett fel i frågan, och ett nytt försök direkt ändrar "
        "inget. Sök eller läs avgörandet direkt på https://hudoc.echr.coe.int i en "
        "webbläsare, eller försök igen senare."
    )


def _kontrollera_paus() -> None:
    """Kastar BotskyddFel utan nätverksanrop om pausen efter en blockering pågår."""
    with _blockerad_las:
        sedan = _blockerad_sedan
    if sedan is not None and time.time() - sedan < _BOTSKYDD_PAUS_SEKUNDER:
        raise BotskyddFel(_meddelande(None, sedan))


def hamta(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET mot HUDOC som känner igen botkontrollen.

    Kastar BotskyddFel om HUDOC svarar med en Cloudflare-utmaning, eller om
    en sådan setts nyligen. Övriga svar returneras som de är; anroparen
    hanterar statuskoder själv.
    """
    global _blockerad_sedan
    _kontrollera_paus()
    svar = session.get(url, **kwargs)
    if _ar_botskydd(svar):
        nu = time.time()
        with _blockerad_las:
            _blockerad_sedan = nu
        raise BotskyddFel(_meddelande(svar.status_code, nu))
    return svar


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

    Kastar BotskyddFel vid botkontroll och requests.HTTPError vid övriga fel.
    """
    url = (
        f"{SOK_URL}"
        f"?query={urllib.parse.quote(query)}"
        f"&select={SELECT_FALT}"
        f"&rankingModelId={RANKING_MODEL_ID}"
        f"&sort="
        f"&start={start}&length={antal}"
    )
    svar = hamta(session, url, timeout=timeout)
    svar.raise_for_status()
    return svar.json()
