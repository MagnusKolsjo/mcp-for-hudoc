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

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent.resolve()

# VIKTIGT: load_dotenv() MÅSTE köras FÖRE "import db" och "import hudoc_query",
# som läser sin konfiguration ur miljön.
load_dotenv(_SCRIPT_DIR / ".env")

import requests
from bs4 import BeautifulSoup
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from typing_extensions import TypedDict

import db
import hudoc_query
from hudoc_query import _HUDOC_BAS_QUERY, BotskyddFel
from mcp_annotationer import CACHE_HINTAR, LASNING_EXTERN
from mcp_transport import starta

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

SERVER_VERSION = "2.1.0"

HUDOC_TIMEOUT         = int(os.getenv("HUDOC_TIMEOUT", "30"))
HUDOC_SOKRESULTAT_MAX = int(os.getenv("HUDOC_SOKRESULTAT_MAX", "50"))

# Query-expansion (valfritt) — aktiveras via QUERY_EXPANSION_ENABLED=true i .env.
# Stöder alla OpenAI-kompatibla endpoints (Claude, OpenAI, Ollama, LM Studio).
QUERY_EXPANSION_ENABLED     = os.getenv("QUERY_EXPANSION_ENABLED", "false").lower() == "true"
QUERY_EXPANSION_BASE_URL    = os.getenv("QUERY_EXPANSION_BASE_URL", "")
QUERY_EXPANSION_API_KEY     = os.getenv("QUERY_EXPANSION_API_KEY", "")
QUERY_EXPANSION_MODEL       = os.getenv("QUERY_EXPANSION_MODEL", "")
QUERY_EXPANSION_PROMPT_FILE = os.getenv(
    "QUERY_EXPANSION_PROMPT_FILE",
    str(_SCRIPT_DIR / "prompts" / "expansion_prompt.txt"),
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

# Standardtak för domtext. Ett ECHR-avgörande når över 350 000 tecken; utan tak
# som gäller by default riskerar svaret att bli onödigt stort. Anroparen kan
# höja taket eller sätta 0 för hela texten.
ECHR_MAX_TECKEN = int(os.getenv("ECHR_MAX_TECKEN", "60000"))

def _skar_ut(text, max_tecken: int, fran_tecken: int = 0) -> dict:
    """
    Skär ut ett textutdrag och redovisa alltid vad som kapats.

    Trunkering utan markering är ett tyst datafel — svaret ser ut att vara hela
    innehållet. max_tecken <= 0 betyder ingen trunkering. Klipper på ordgräns.
    """
    text   = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]

    if max_tecken and max_tecken > 0 and len(rest) > max_tecken:
        utdrag    = rest[:max_tecken]
        brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        utdrag    = utdrag.rstrip()
        trunkerad = True
    else:
        utdrag    = rest
        trunkerad = False

    slut = start + len(utdrag)
    return {
        "text":                 utdrag,
        "tecken_totalt":        totalt,
        "tecken_visade":        len(utdrag),
        "trunkerad":            trunkerad,
        "fortsatt_fran_tecken": slut if slut < totalt else None,
    }

# ---------------------------------------------------------------------------
# Svarstyper
#
# Svaren valideras mot typerna. Fält som kan saknas i HUDOC:s data (äldre
# avgöranden saknar ofta ECLI, domsdatum eller slutsats) är därför `| None`.
# ---------------------------------------------------------------------------

class Traff(TypedDict):
    """Metadata för ett avgörande."""
    itemid: str | None
    appno: str | None
    datum: str | None
    publiceringsdatum: str | None
    respondent: str | None
    ecli: str | None
    samling: str | None
    importance: str | None
    artikel: str | None
    slutsats: str | None
    sprak: str | None


class Sokresultat(TypedDict):
    """Svar från echr_search och echr_hamta_svenska_mal."""
    kalla: str
    totalt_antal: int
    start: int
    antal_returnerade: int
    nasta_start: int | None
    expansion: list[str] | None
    resultat: list[Traff]


class Domtext(TypedDict):
    """Svar från echr_hamta_dom."""
    itemid: str
    kalla: str
    sprak: str | None
    antal_tecken: int
    fulltext: str
    tecken_totalt: int
    trunkerad: bool
    fortsatt_fran_tecken: int | None


class EcliSvar(TypedDict):
    """Svar från echr_hitta_via_ecli.

    Fulltextfälten är None när texten inte kunde hämtas; `anmarkning` säger
    då varför.
    """
    metadata: Traff
    fulltext: str | None
    antal_tecken: int | None
    kalla: str | None
    tecken_totalt: int | None
    trunkerad: bool | None
    fortsatt_fran_tecken: int | None
    anmarkning: str | None

# ---------------------------------------------------------------------------
# MCP-server
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "echr-hudoc",
    instructions=(
        "MCP-server för Europadomstolens (ECHR) avgöranden via HUDOC. "
        "Verktygen har prefixet echr_. ARBETSORDNING: echr_search eller "
        "echr_hamta_svenska_mal ger metadata med itemid; echr_hamta_dom hämtar "
        "fulltexten för ett itemid. Har du ett ECLI, använd echr_hitta_via_ecli. "
        "FILTER: ar_fran/ar_till filtrerar på publiceringsdatum i HUDOC (kpdate), "
        "inte på domsdatum. TRUNKERING: echr_hamta_dom kapar vid max_tecken "
        "(standard 60 000); ett kapat svar bär trunkerad, tecken_totalt och "
        "fortsatt_fran_tecken. Citera aldrig ordagrant ur ett kapat svar utan att "
        "läsa vidare med fran_tecken. BOTSKYDD: HUDOC kan blockera automatiserade "
        "anrop med Cloudflares botkontroll. Verktygen svarar då med ett fel som "
        "säger det; det är inte ett fel i frågan. Hänvisa användaren till "
        "https://hudoc.echr.coe.int i stället för att försöka igen direkt."
    ),
    version=SERVER_VERSION,
    cache_hints=CACHE_HINTAR,
)

# ---------------------------------------------------------------------------
# HUDOC-hjälpfunktioner
# ---------------------------------------------------------------------------

# En session för hela processen. Den skapas vid import och delas mellan
# verktygsanropen; headers ändras aldrig efter start.
_SESSION = hudoc_query.skapa_session()


def expandera_fraga(query: str) -> list[str]:
    """Expanderar söktermen med flerspråkiga MR-juridiska ekvivalenter via LLM.

    Returnerar kompletterande söktermer på engelska, franska och svenska —
    eller tom lista om expansion är inaktiverat eller misslyckas.

    Aktiveras via QUERY_EXPANSION_ENABLED=true i .env.
    Promptfilen (prompts/expansion_prompt.txt) kan redigeras fritt.
    """
    if not QUERY_EXPANSION_ENABLED:
        return []

    prompt_path = Path(QUERY_EXPANSION_PROMPT_FILE)
    if not prompt_path.exists():
        log.warning("Promptfil för query-expansion saknas: %s", prompt_path)
        return []

    try:
        from openai import OpenAI

        prompt_mall = prompt_path.read_text(encoding="utf-8")
        prompt = prompt_mall.replace("{query}", query)

        klient = OpenAI(
            base_url=QUERY_EXPANSION_BASE_URL or None,
            api_key=QUERY_EXPANSION_API_KEY or "placeholder",
        )
        svar = klient.chat.completions.create(
            model=QUERY_EXPANSION_MODEL or "claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.1,
        )
        raw   = svar.choices[0].message.content.strip()
        terms = [t.strip() for t in raw.split(",") if t.strip()]
        log.info("Query-expansion: %r → %s", query, terms)
        return terms[:10]

    except Exception as exc:
        log.warning("Query-expansion misslyckades (fortsätter utan): %s", exc)
        return []


def _bygg_query(
    soktermer: list[str],
    respondent: str | None = None,
    artikel: int | None = None,
    importance: int | None = None,
    ar_fran: int | None = None,
    ar_till: int | None = None,
    samling: str | None = None,
) -> str:
    """Bygger den fullständiga HUDOC-query-strängen med obligatorisk bas + extra filter.

    HUDOC:s results-endpoint kräver bas-queryn med XRANK-struktur och rankingModelId.
    Extra filter läggs till med AND.

    soktermer är en lista med OR-termer (manuella + LLM-genererade).
    Flervordiga fraser citeras automatiskt för korrekt FAST-sökning.
    """
    extra: list[str] = []

    if soktermer:
        # Bygg OR-block: flervordiga fraser citeras, enkla ord lämnas nakna.
        or_delar = [f'"{t}"' if " " in t else t for t in soktermer]
        extra.append(f"({' OR '.join(or_delar)})")

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
        # documentcollectionid2 med citattecken är rätt fält för samlingsvärden
        # som GRANDCHAMBER, CHAMBER, DECISIONS, JUDGMENTS, COMMUNICATEDCASES, CLIN.
        # doctypebranch=VÄRDE (utan citattecken) ger 0 träffar för de flesta värden.
        extra.append(f'documentcollectionid2:"{samling.upper()}"')

    if extra:
        # OBS: Lägg INTE extra parenteser runt bas-queryn — XRANK-syntaxen bryts då.
        return f"{_HUDOC_BAS_QUERY} AND ({' AND '.join(extra)})"
    return _HUDOC_BAS_QUERY


def _hudoc_sok_live(query: str, start: int = 0, antal: int = 20) -> dict:
    """Kör en sökning live mot HUDOC med processens delade session.

    Kastar BotskyddFel vid botkontroll och requests-undantag vid övriga fel.
    """
    return hudoc_query.sok(_SESSION, query, start=start, antal=antal, timeout=HUDOC_TIMEOUT)


def _hudoc_fel(fel: requests.RequestException) -> ToolError:
    """Översätter ett nätverks- eller HTTP-fel från HUDOC till ett ToolError."""
    if isinstance(fel, requests.HTTPError) and fel.response is not None:
        return ToolError(
            f"HUDOC svarade med HTTP {fel.response.status_code}. Felet ligger hos "
            "källan, inte i frågan. Försök igen senare, eller sök direkt på "
            "https://hudoc.echr.coe.int."
        )
    return ToolError(
        f"HUDOC gick inte att nå ({type(fel).__name__}). Försök igen senare, "
        "eller sök direkt på https://hudoc.echr.coe.int."
    )


def _iso_datum(s: str | None) -> str | None:
    """Parsar HUDOC-datumsträngar (DD/MM/YYYY HH:MM:SS eller ISO) till YYYY-MM-DD."""
    from datetime import datetime
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return s.strip() or None


def _formattera_sokresultat(hudoc_rader: list[dict]) -> list[Traff]:
    """Konverterar HUDOC-rådata till ett rent svarsformat.

    datum           = domsdatum (judgementdate) på ISO-format YYYY-MM-DD
    publiceringsdatum = publiceringsdatum i HUDOC (kpdate) på ISO-format
    OBS: ar_fran/ar_till i sökverktygen filtrerar på publiceringsdatum (kpdate),
    inte på domsdatum.
    """
    resultat: list[Traff] = []
    for rad in hudoc_rader:
        kol = rad.get("columns", {})
        resultat.append({
            "itemid":            kol.get("itemid", ""),
            "appno":             kol.get("appno", ""),
            "datum":             _iso_datum(kol.get("judgementdate")),
            "publiceringsdatum": _iso_datum(kol.get("kpdate")),
            "respondent":        kol.get("respondent", ""),
            "ecli":              kol.get("ecli", ""),
            "samling":           kol.get("doctypebranch", ""),
            "importance":        kol.get("importance", ""),
            "artikel":           kol.get("article", ""),
            "slutsats":          kol.get("conclusion", ""),
            "sprak":             kol.get("languageisocode", ""),
        })
    return resultat


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

    Kastar BotskyddFel vid botkontroll, requests.HTTPError vid övriga
    HTTP-fel och ValueError om ingen text finns på något språk.
    """
    for sprak in _SPRAK_PRIORITET:
        params: dict = {"library": "ECHR", "id": itemid}
        if sprak is not None:
            params["language"] = sprak

        svar = hudoc_query.hamta(
            _SESSION,
            hudoc_query.FULLTEXT_URL,
            params=params,
            timeout=HUDOC_TIMEOUT,
        )

        if svar.status_code == 404:
            # Detta språk finns inte — prova nästa
            log.debug("Ingen fulltext för itemid=%s sprak=%s (404)", itemid, sprak)
            continue

        svar.raise_for_status()
        soup = BeautifulSoup(svar.text, "html.parser")
        text = soup.get_text(separator="\n", strip=True)

        if text.strip():
            anvant_sprak = sprak if sprak is not None else "okänt"
            log.info(
                "Fulltext för itemid=%s hämtad på språk=%s (%d tecken)",
                itemid, anvant_sprak, len(text),
            )
            return text, anvant_sprak

    raise ValueError(f"Ingen fulltext hittades för itemid={itemid!r} på något språk.")


def _spara_metadata_fran_hudoc(itemid: str) -> None:
    """Hämtar och sparar metadata för ett avgörande som inte synkats än.

    Metadata är ett tillägg till fulltexten; fel här loggas men stoppar inte
    svaret.
    """
    if db.hamta_avgorande(itemid):
        return
    try:
        svar = _hudoc_sok_live(f"{_HUDOC_BAS_QUERY} AND (itemid={itemid})", antal=1)
        rader = svar.get("results", [])
        if not rader:
            return
        kol = rader[0].get("columns", {})
        db.spara_avgorande({
            "itemid":            kol.get("itemid", itemid),
            "appno":             kol.get("appno", ""),
            "domsdatum":         _iso_datum(kol.get("judgementdate")),
            "publiceringsdatum": _iso_datum(kol.get("kpdate")),
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


def _hamta_dom(itemid: str, max_tecken: int, fran_tecken: int) -> Domtext:
    """Gemensam kärna för echr_hamta_dom och echr_hitta_via_ecli.

    Lokal cache först, sedan HUDOC. Kastar ToolError när texten inte kan
    levereras.
    """
    cachad = db.hamta_fulltext_fran_cache(itemid)
    if cachad:
        log.info("echr_hamta_dom: serverar %s från cache", itemid)
        # Hämta sprak från metadata-cachen för konsekvent svarstruktur
        meta = db.hamta_avgorande(itemid)
        # Cachen har alltid hela texten — trunkeringen gäller bara svaret.
        u = _skar_ut(cachad, max_tecken, fran_tecken)
        return {
            "itemid":               itemid,
            "kalla":                "cache",
            "sprak":                (meta.get("sprak") or None) if meta else None,
            "antal_tecken":         u["tecken_visade"],
            "fulltext":             u["text"],
            "tecken_totalt":        u["tecken_totalt"],
            "trunkerad":            u["trunkerad"],
            "fortsatt_fran_tecken": u["fortsatt_fran_tecken"],
        }

    try:
        text, anvant_sprak = _hamta_fulltext_fran_hudoc(itemid)
    except BotskyddFel as e:
        raise ToolError(f"{e} Avgörandet {itemid} finns inte i den lokala fulltextcachen.") from e
    except ValueError as e:
        raise ToolError(
            f"Ingen fulltext hittades för itemid {itemid!r} på något språk. "
            "Kontrollera id:t med echr_search."
        ) from e
    except requests.RequestException as e:
        log.error("echr_hamta_dom fel för %s: %s", itemid, e)
        raise _hudoc_fel(e) from e

    _spara_metadata_fran_hudoc(itemid)
    db.spara_fulltext(itemid, text)

    u = _skar_ut(text, max_tecken, fran_tecken)
    return {
        "itemid":               itemid,
        "kalla":                "hudoc",
        "sprak":                anvant_sprak,
        "antal_tecken":         u["tecken_visade"],
        "fulltext":             u["text"],
        "tecken_totalt":        u["tecken_totalt"],
        "trunkerad":            u["trunkerad"],
        "fortsatt_fran_tecken": u["fortsatt_fran_tecken"],
    }


# ---------------------------------------------------------------------------
# MCP-verktyg
# ---------------------------------------------------------------------------

@mcp.tool(title="Sök ECHR-avgöranden i HUDOC", annotations=LASNING_EXTERN)
def echr_search(
    fritextsokning: str | None = None,
    respondent: str | None = None,
    artikel: int | None = None,
    importance: int | None = None,
    ar_fran: int | None = None,
    ar_till: int | None = None,
    samling: str | None = None,
    start: int = 0,
    antal: int = 20,
) -> Sokresultat:
    """Söker i HUDOC (Europadomstolens databas) med valfria filter.

    Returnerar metadata för matchande avgöranden — inte fulltext.
    Använd echr_hamta_dom för att hämta fulltext för ett specifikt avgörande.

    Parametrar:
      fritextsokning  Fritext mot titel och slutsats. Kommaseparerade termer
                      behandlas som OR (t.ex. "privatliv, private life, vie privée").
                      Om QUERY_EXPANSION_ENABLED=true i .env expanderas söktermen
                      automatiskt med flerspråkiga ekvivalenter via LLM.
      respondent      Svarandestat som tre-bokstavs ISO-kod, t.ex. "SWE", "DEU", "FRA"
      artikel         Artikel i Europakonventionen, t.ex. 6 (rättvis rättegång),
                      8 (privatliv), 10 (yttrandefrihet)
      importance      Prioritet: 1=hög, 2=medel, 3=låg
      ar_fran         Publiceringsår i HUDOC fr.o.m. (kpdate, t.ex. 2010).
                      OBS: filtrerar på publiceringsdatum, inte domsdatum.
      ar_till         Publiceringsår i HUDOC t.o.m. (kpdate, t.ex. 2024)
      samling         Dokumentsamling (documentcollectionid2-värden):
                      GRANDCHAMBER (stor kammaredomar), CHAMBER (kammaredomar),
                      JUDGMENTS (alla domar inkl. äldre), DECISIONS (beslut),
                      COMMUNICATEDCASES (kommunicerade mål), CLIN (sammanfattningar)
      start           Sidnumrering: startindex (standard 0)
      antal           Antal resultat att returnera (standard 20, max 50)
    """
    antal = min(antal, HUDOC_SOKRESULTAT_MAX)

    # Bygg lista med alla söktermer (OR-logik).
    # Kommaseparerade termer i fritextsokning bevaras som fraser — ingen split på mellanslag.
    if fritextsokning:
        if "," in fritextsokning:
            soktermer_delar = [t.strip() for t in fritextsokning.split(",") if t.strip()]
        else:
            soktermer_delar = [fritextsokning.strip()]
    else:
        soktermer_delar = []

    # Query-expansion: flerspråkiga ekvivalenter via valfritt LLM-anrop.
    extra_termer = expandera_fraga(fritextsokning) if fritextsokning else []
    alla_termer  = soktermer_delar + extra_termer

    query = _bygg_query(
        soktermer=alla_termer,
        respondent=respondent,
        artikel=artikel,
        importance=importance,
        ar_fran=ar_fran,
        ar_till=ar_till,
        samling=samling,
    )

    log.info("echr_search: soktermer=%s expansion=%s start=%d antal=%d",
             soktermer_delar, extra_termer, start, antal)

    try:
        svar = _hudoc_sok_live(query, start=start, antal=antal)
    except BotskyddFel as e:
        raise ToolError(str(e)) from e
    except requests.RequestException as e:
        log.error("echr_search fel: %s", e)
        raise _hudoc_fel(e) from e

    rader = svar.get("results", [])
    totalt = svar.get("resultcount", 0)

    return {
        "kalla": "hudoc",
        "totalt_antal": totalt,
        "start": start,
        "antal_returnerade": len(rader),
        "nasta_start": start + len(rader) if start + len(rader) < totalt else None,
        "expansion": extra_termer if extra_termer else None,
        "resultat": _formattera_sokresultat(rader),
    }


@mcp.tool(title="Hämta fulltext för ett ECHR-avgörande", annotations=LASNING_EXTERN)
def echr_hamta_dom(
    itemid: str,
    max_tecken: int = ECHR_MAX_TECKEN,
    fran_tecken: int = 0,
) -> Domtext:
    """Hämtar fulltext för ett ECHR-avgörande via dess itemid.

    Fulltexten hämtas on-demand från HUDOC och cachas lokalt i databasen
    för snabbare åtkomst vid framtida anrop.

    Parametrar:
      itemid   HUDOC-internt id, t.ex. "001-57548" (Olsson v. Sweden, 1988)
               eller "001-60487" (Lindqvist v. Sweden)

    Tips: Använd echr_search eller echr_hamta_svenska_mal för att hitta itemid.
    """
    log.info("echr_hamta_dom: itemid=%s", itemid)
    return _hamta_dom(itemid, max_tecken, fran_tecken)


@mcp.tool(title="Svenska mål i Europadomstolen", annotations=LASNING_EXTERN)
def echr_hamta_svenska_mal(
    ar_fran: int | None = None,
    ar_till: int | None = None,
    importance: int | None = None,
    artikel: int | None = None,
    samling: str | None = None,
    start: int = 0,
    antal: int = 20,
) -> Sokresultat:
    """Söker bland ECHR-avgöranden med Sverige som svarandestat (respondent=SWE).

    Bekvämlighetsverktyg för att snabbt hitta svenska mål utan att ange
    respondent=SWE explicit. Alla parametrar är valfria.

    Parametrar:
      ar_fran    Publiceringsår i HUDOC fr.o.m. (kpdate — OBS: ej domsdatum)
      ar_till    Publiceringsår i HUDOC t.o.m.
      importance Prioritet: 1=hög (~173 svenska mål), 2=medel, 3=låg
      artikel    Artikel i Europakonventionen, t.ex. 6, 8, 10
      samling    GRANDCHAMBER, CHAMBER, JUDGMENTS, DECISIONS, COMMUNICATEDCASES, CLIN
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


@mcp.tool(title="Slå upp ECHR-avgörande via ECLI", annotations=LASNING_EXTERN)
def echr_hitta_via_ecli(ecli: str) -> EcliSvar:
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
    except BotskyddFel as e:
        raise ToolError(str(e)) from e
    except requests.RequestException as e:
        log.error("echr_hitta_via_ecli fel: %s", e)
        raise _hudoc_fel(e) from e

    if not rader:
        raise ToolError(f"Inget avgörande hittades för ECLI {ecli!r}. Kontrollera formatet.")

    metadata = _formattera_sokresultat(rader)[0]
    itemid = metadata["itemid"]
    if not itemid:
        raise ToolError(f"Avgörandet med ECLI {ecli!r} saknar itemid i HUDOC; fulltexten kan inte hämtas.")

    return _ecli_svar(metadata, itemid)


def _ecli_svar(metadata: Traff, itemid: str) -> EcliSvar:
    """Kompletterar metadata med fulltext. Ett fel på fulltexten blir en anmärkning."""
    try:
        dom = _hamta_dom(itemid, ECHR_MAX_TECKEN, 0)
    except ToolError as e:
        return {
            "metadata": metadata,
            "fulltext": None,
            "antal_tecken": None,
            "kalla": None,
            "tecken_totalt": None,
            "trunkerad": None,
            "fortsatt_fran_tecken": None,
            "anmarkning": f"Fulltexten kunde inte hämtas: {e}",
        }
    return {
        "metadata": metadata,
        "fulltext": dom["fulltext"],
        "antal_tecken": dom["antal_tecken"],
        "kalla": dom["kalla"],
        "tecken_totalt": dom["tecken_totalt"],
        "trunkerad": dom["trunkerad"],
        "fortsatt_fran_tecken": dom["fortsatt_fran_tecken"],
        "anmarkning": None,
    }


# ---------------------------------------------------------------------------
# Startpunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Startar echr-hudoc MCP-server")
    starta(mcp, standardport=8011, initiera=db.initiera_schema)
