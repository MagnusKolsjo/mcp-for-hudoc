"""
mcp_server.py — MCP-server för ECHR-praxis via HUDOC.

Fyra verktyg:
  echr_search              — Söker i HUDOC med valfria filter
  echr_hamta_dom           — Hämtar fulltext för ett avgörande (on-demand, cachar i DB)
  echr_hamta_svenska_mal   — Bekvämlighetsverktyg: söker med respondent=SWE
  echr_hitta_via_ecli      — Hämtar metadata och fulltext via ECLI

Datakälla: HUDOC — Europadomstolens för mänskliga rättigheters officiella databas.
  Sökning:  https://hudoc.echr.coe.int/app/query/results
  Fulltext: https://hudoc.echr.coe.int/app/conversion/docx/html/body

Transport styrs via MCP_TRANSPORT i .env: stdio (standard) eller http.
"""

from __future__ import annotations

import contextlib
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# VIKTIGT: load_dotenv() MÅSTE köras FÖRE "import db"
load_dotenv()

import requests
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

import db

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent.resolve()

MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST      = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT      = int(os.getenv("MCP_PORT", "8011"))
MCP_API_KEY   = os.getenv("MCP_API_KEY", "")

HUDOC_TIMEOUT         = int(os.getenv("HUDOC_TIMEOUT", "30"))
HUDOC_SOKRESULTAT_MAX = int(os.getenv("HUDOC_SOKRESULTAT_MAX", "50"))

# HUDOC kräver rankingModelId och HTTP (ej HTTPS) för results-endpointen.
RANKING_MODEL_ID = "4180000c-8692-45ca-ad63-74bc4163871b"

# Bas-query med obligatorisk XRANK-struktur.
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

FULLTEXT_CACHE_DIR = Path(
    os.getenv("ECHR_FULLTEXT_CACHE_DIR", "") or str(_SCRIPT_DIR / "fulltext_cache")
)
if not FULLTEXT_CACHE_DIR.is_absolute():
    FULLTEXT_CACHE_DIR = _SCRIPT_DIR / FULLTEXT_CACHE_DIR
FULLTEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

SELECT_FALT = (
    "itemid,appno,judgementdate,kpdate,respondent,ecli,"
    "doctypebranch,importance,article,conclusion,languageisocode,typedescription"
)

# ---------------------------------------------------------------------------
# Loggning (till fil — stdout är reserverat för MCP-protokollet i stdio-läge)
# ---------------------------------------------------------------------------

def _konfigurera_logging() -> None:
    log_mapp = _SCRIPT_DIR / "logs"
    log_mapp.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_mapp / "mcp_server.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        encoding="utf-8",
    )

_konfigurera_logging()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP-server
# ---------------------------------------------------------------------------

mcp = FastMCP("echr-hudoc")

# ---------------------------------------------------------------------------
# HUDOC-hjälpfunktioner
# ---------------------------------------------------------------------------

_hudoc_session: Optional[requests.Session] = None


def _hamta_session() -> requests.Session:
    """Returnerar en requests-session med rätt HUDOC-headers (lazy, delad)."""
    global _hudoc_session
    if _hudoc_session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _hudoc_session = s
        log.info("HUDOC-session initierad")
    return _hudoc_session


def _bygg_query(
    fritextsokning: Optional[str] = None,
    respondent: Optional[str] = None,
    artikel: Optional[int] = None,
    importance: Optional[int] = None,
    ar_fran: Optional[int] = None,
    ar_till: Optional[int] = None,
    samling: Optional[str] = None,
) -> str:
    """Bygger den fullständiga HUDOC-query-strängen med obligatorisk bas + extra filter.

    HUDOC:s results-endpoint kräver bas-queryn med XRANK-struktur och rankingModelId.
    Extra filter läggs till med AND.
    """
    extra: list[str] = []

    if fritextsokning:
        extra.append(fritextsokning)

    if respondent:
        extra.append(f"respondent={respondent.upper()}")

    if artikel is not None:
        extra.append(f"article={artikel}")

    if importance is not None:
        extra.append(f"importance={importance}")

    if ar_fran and ar_till:
        extra.append(f"kpdate>=\"{ar_fran}-01-01\" AND kpdate<=\"{ar_till}-12-31\"")
    elif ar_fran:
        extra.append(f"kpdate>=\"{ar_fran}-01-01\"")
    elif ar_till:
        extra.append(f"kpdate<=\"{ar_till}-12-31\"")

    if samling:
        extra.append(f"doctypebranch={samling.upper()}")

    if extra:
        # OBS: Lägg INTE extra parenteser runt bas-queryn — XRANK-syntaxen bryts då.
        return f"{_HUDOC_BAS_QUERY} AND ({' AND '.join(extra)})"
    return _HUDOC_BAS_QUERY


def _hudoc_sok_live(query: str, start: int = 0, antal: int = 20) -> dict:
    """Kör en sökning live mot HUDOC.

    Kräver HTTP (ej HTTPS) och rankingModelId — utan dessa returneras 404.
    """
    session = _hamta_session()
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


def _formattera_sokresultat(hudoc_rader: list[dict]) -> list[dict]:
    """Konverterar HUDOC-rådata till ett rent svarsformat."""
    resultat = []
    for rad in hudoc_rader:
        kol = rad.get("columns", {})
        resultat.append({
            "itemid":       kol.get("itemid", ""),
            "appno":        kol.get("appno", ""),
            "datum":        kol.get("judgementdate", ""),
            "respondent":   kol.get("respondent", ""),
            "ecli":         kol.get("ecli", ""),
            "samling":      kol.get("doctypebranch", ""),
            "importance":   kol.get("importance", ""),
            "artikel":      kol.get("article", ""),
            "slutsats":     kol.get("conclusion", ""),
            "sprak":        kol.get("languageisocode", ""),
        })
    return resultat


@contextlib.contextmanager
def _tysta_fd1():
    """OS-nivå omdirigering av FD 1 till loggfil under HTML-hämtning.

    Skyddar MCP-protokollets stdout från C-biblioteks diagnostikutskrifter.
    Mönster etablerat i arbetsström 9 (gov-dokument/pdf_lib.py).
    """
    log_path = _SCRIPT_DIR / "logs" / "subprocess.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    save_out = os.dup(1)
    save_err = os.dup(2)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        yield
    finally:
        os.dup2(save_out, 1)
        os.dup2(save_err, 2)
        os.close(save_out)
        os.close(save_err)
        os.close(log_fd)


# Språkprioriteringsordning för fulltexthämtning.
# De flesta ECHR-domar finns på engelska och/eller franska — sällan på svenska.
# Prioritetsordningen: SV → EN → FR → DE → ES → IT → tillgängligt språk (None).
# "None" som sista element innebär att HUDOC själv väljer tillgängligt språk.
_SPRAK_PRIORITET = ["SWE", "ENG", "FRE", "GER", "SPA", "ITA", None]


def _hamta_fulltext_fran_hudoc(itemid: str) -> tuple[str, str]:
    """Hämtar fulltext som ren text från HUDOC med språkfallback.

    Provar språk i prioritetsordningen SV→EN→FR→DE→ES→IT→tillgängligt.
    Returnerar tuple (text, sprak_kod) där sprak_kod är det faktiska språket
    som användes, t.ex. "ENG" eller "FRE".

    Höjer requests.HTTPError vid nätverksfel.
    """
    session = _hamta_session()

    for sprak in _SPRAK_PRIORITET:
        params: dict = {"library": "ECHR", "id": itemid}
        if sprak is not None:
            params["language"] = sprak

        with _tysta_fd1():
            svar = session.get(
                "https://hudoc.echr.coe.int/app/conversion/docx/html/body",
                params=params,
                timeout=HUDOC_TIMEOUT,
            )

        if svar.status_code == 404:
            # Detta språk finns inte — prova nästa
            log.debug("Ingen fulltext för itemid=%s sprak=%s (404)", itemid, sprak)
            continue

        svar.raise_for_status()
        html = svar.text

        with _tysta_fd1():
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator="\n", strip=True)

        if text.strip():
            anvant_sprak = sprak if sprak is not None else "okänt"
            log.info(
                "Fulltext för itemid=%s hämtad på språk=%s (%d tecken)",
                itemid, anvant_sprak, len(text),
            )
            return text, anvant_sprak

    raise ValueError(f"Ingen fulltext hittades för itemid={itemid!r} på något språk.")


# ---------------------------------------------------------------------------
# MCP-verktyg
# ---------------------------------------------------------------------------

@mcp.tool()
def echr_search(
    fritextsokning: Optional[str] = None,
    respondent: Optional[str] = None,
    artikel: Optional[int] = None,
    importance: Optional[int] = None,
    ar_fran: Optional[int] = None,
    ar_till: Optional[int] = None,
    samling: Optional[str] = None,
    start: int = 0,
    antal: int = 20,
) -> dict:
    """Söker i HUDOC (Europadomstolens databas) med valfria filter.

    Returnerar metadata för matchande avgöranden — inte fulltext.
    Använd echr_hamta_dom för att hämta fulltext för ett specifikt avgörande.

    Parametrar:
      fritextsokning  Fritext mot titel och slutsats
      respondent      Svarandestat som tre-bokstavs ISO-kod, t.ex. "SWE", "DEU", "FRA"
      artikel         Artikel i Europakonventionen, t.ex. 6 (rättvis rättegång),
                      8 (privatliv), 10 (yttrandefrihet)
      importance      Prioritet: 1=hög, 2=medel, 3=låg
      ar_fran         Publiceringsår fr.o.m. (t.ex. 2010)
      ar_till         Publiceringsår t.o.m. (t.ex. 2024)
      samling         Dokumentsamling: GRANDCHAMBER, CHAMBER, DECISIONS,
                      JUDGMENTS, COMMUNICATEDCASES, CLIN
      start           Sidnumrering: startindex (standard 0)
      antal           Antal resultat att returnera (standard 20, max 50)
    """
    antal = min(antal, HUDOC_SOKRESULTAT_MAX)
    query = _bygg_query(
        fritextsokning=fritextsokning,
        respondent=respondent,
        artikel=artikel,
        importance=importance,
        ar_fran=ar_fran,
        ar_till=ar_till,
        samling=samling,
    )

    log.info("echr_search: query=%s start=%d antal=%d", query[:100], start, antal)

    try:
        svar = _hudoc_sok_live(query, start=start, antal=antal)
    except requests.HTTPError as e:
        return {"fel": f"HUDOC svarade med HTTP-fel: {e}", "resultat": []}
    except Exception as e:
        log.error("echr_search fel: %s", e)
        return {"fel": str(e), "resultat": []}

    rader = svar.get("results", [])
    totalt = svar.get("resultcount", 0)

    return {
        "totalt_antal": totalt,
        "start": start,
        "antal_returnerade": len(rader),
        "nasta_start": start + len(rader) if start + len(rader) < totalt else None,
        "resultat": _formattera_sokresultat(rader),
    }


@mcp.tool()
def echr_hamta_dom(itemid: str) -> dict:
    """Hämtar fulltext för ett ECHR-avgörande via dess itemid.

    Fulltexten hämtas on-demand från HUDOC och cachas lokalt i databasen
    för snabbare åtkomst vid framtida anrop.

    Parametrar:
      itemid   HUDOC-internt id, t.ex. "001-57548" (Olsson v. Sweden, 1988)
               eller "001-60487" (Lindqvist v. Sweden)

    Tips: Använd echr_search eller echr_hamta_svenska_mal för att hitta itemid.
    """
    log.info("echr_hamta_dom: itemid=%s", itemid)

    # Kontrollera lokal cache först
    cachad = db.hamta_fulltext_fran_cache(itemid)
    if cachad:
        log.info("echr_hamta_dom: serverar från cache")
        return {
            "itemid": itemid,
            "kalla": "cache",
            "antal_tecken": len(cachad),
            "fulltext": cachad,
        }

    # Hämta från HUDOC med språkfallback
    try:
        text, anvant_sprak = _hamta_fulltext_fran_hudoc(itemid)
    except requests.HTTPError as e:
        return {"fel": f"HUDOC svarade med HTTP-fel: {e}"}
    except ValueError as e:
        return {"fel": str(e)}
    except Exception as e:
        log.error("echr_hamta_dom fel för %s: %s", itemid, e)
        return {"fel": str(e)}

    # Säkerställ att metadata finns i avgorande_cache (om den inte synkats än)
    if not db.hamta_avgorande(itemid):
        try:
            svar = _hudoc_sok_live(f"{_HUDOC_BAS_QUERY} AND (itemid={itemid})", antal=1)
            rader = svar.get("results", [])
            if rader:
                from datetime import datetime
                kol = rader[0].get("columns", {})

                def _datum(s):
                    if not s:
                        return None
                    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                        try:
                            return datetime.strptime(s.strip(), fmt).date().isoformat()
                        except ValueError:
                            continue
                    return s.strip() or None

                db.spara_avgorande({
                    "itemid":            kol.get("itemid", itemid),
                    "appno":             kol.get("appno", ""),
                    "domsatum":          _datum(kol.get("judgementdate")),
                    "publiceringsdatum": _datum(kol.get("kpdate")),
                    "svarandestat":      kol.get("respondent", ""),
                    "ecli":              kol.get("ecli", ""),
                    "samling":           kol.get("doctypebranch", ""),
                    "importance":        int(kol["importance"]) if kol.get("importance") else None,
                    "artikel":           kol.get("article", ""),
                    "slutsats":          kol.get("conclusion", ""),
                    "sprak":             kol.get("languageisocode", ""),
                    "typbeskrivning":    kol.get("typedescription", ""),
                })
        except Exception as e:
            log.warning("Kunde inte spara metadata för %s: %s", itemid, e)

    # Cacha fulltexten
    db.spara_fulltext(itemid, text)

    return {
        "itemid": itemid,
        "kalla": "hudoc",
        "sprak": anvant_sprak,
        "antal_tecken": len(text),
        "fulltext": text,
    }


@mcp.tool()
def echr_hamta_svenska_mal(
    ar_fran: Optional[int] = None,
    ar_till: Optional[int] = None,
    importance: Optional[int] = None,
    artikel: Optional[int] = None,
    samling: Optional[str] = None,
    start: int = 0,
    antal: int = 20,
) -> dict:
    """Söker bland ECHR-avgöranden med Sverige som svarandestat (respondent=SWE).

    Bekvämlighetsverktyg för att snabbt hitta svenska mål utan att ange
    respondent=SWE explicit. Alla parametrar är valfria.

    Parametrar:
      ar_fran    Publiceringsår fr.o.m.
      ar_till    Publiceringsår t.o.m.
      importance Prioritet: 1=hög (~173 svenska mål), 2=medel, 3=låg
      artikel    Artikel i Europakonventionen, t.ex. 6, 8, 10
      samling    GRANDCHAMBER, CHAMBER, DECISIONS m.fl.
      start      Sidnumrering: startindex
      antal      Antal resultat (standard 20, max 50)
    """
    return echr_search(
        respondent="SWE",
        ar_fran=ar_fran,
        ar_till=ar_till,
        importance=importance,
        artikel=artikel,
        samling=samling,
        start=start,
        antal=antal,
    )


@mcp.tool()
def echr_hitta_via_ecli(ecli: str) -> dict:
    """Hämtar metadata och fulltext för ett ECHR-avgörande via ECLI.

    ECLI (European Case Law Identifier) för ECHR har formatet:
      ECLI:CE:ECHR:ÅÅÅÅ:MMDDTYP######

    Exempel: ECLI:CE:ECHR:1988:0324JUD001046583  (Olsson v. Sweden)

    Tips: ECLI-numret hittas i echr_search-resultaten under fältet "ecli".
    OBS: ECLI förekommer sällan i svenska domtexter — ansökningsnumret
    (appno) är vanligare och kan sökas med echr_search.
    """
    log.info("echr_hitta_via_ecli: ecli=%s", ecli)

    # Sök upp itemid via ECLI
    try:
        query = f'{_HUDOC_BAS_QUERY} AND (ecli="{ecli}")'
        svar = _hudoc_sok_live(query, antal=1)
        rader = svar.get("results", [])
    except Exception as e:
        log.error("echr_hitta_via_ecli fel: %s", e)
        return {"fel": str(e)}

    if not rader:
        return {"fel": f"Inget avgörande hittades för ECLI: {ecli!r}"}

    kol = rader[0].get("columns", {})
    itemid = kol.get("itemid", "")
    if not itemid:
        return {"fel": "Avgörandet saknar itemid — kan inte hämta fulltext."}

    metadata = _formattera_sokresultat(rader)[0]

    # Hämta fulltext via echr_hamta_dom
    fulltext_svar = echr_hamta_dom(itemid)

    return {
        "metadata": metadata,
        "fulltext": fulltext_svar.get("fulltext"),
        "antal_tecken": fulltext_svar.get("antal_tecken"),
        "kalla": fulltext_svar.get("kalla"),
        "fel": fulltext_svar.get("fel"),
    }


# ---------------------------------------------------------------------------
# HTTP-transport: Bearer-token-autentisering
# ---------------------------------------------------------------------------

def _skapa_http_app():
    """Skapar en Starlette-app med Bearer-token-middleware för HTTP-läge."""
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class BearerTokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if MCP_API_KEY:
                auth = request.headers.get("Authorization", "")
                if auth != f"Bearer {MCP_API_KEY}":
                    return JSONResponse(
                        {"error": "Ogiltig eller saknad API-nyckel"},
                        status_code=401,
                    )
            return await call_next(request)

    app = mcp.streamable_http_app()
    app.add_middleware(BearerTokenMiddleware)
    return app


# ---------------------------------------------------------------------------
# Startpunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Startar echr-hudoc MCP-server (transport=%s)", MCP_TRANSPORT)

    # Initiera databas (fel loggas men stoppar inte servern)
    db.initiera_schema()

    if MCP_TRANSPORT == "http":
        import uvicorn
        app = _skapa_http_app()
        log.info("HTTP-läge: lyssnar på %s:%d", MCP_HOST, MCP_PORT)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
    else:
        log.info("stdio-läge: startar")
        mcp.run(transport="stdio")
