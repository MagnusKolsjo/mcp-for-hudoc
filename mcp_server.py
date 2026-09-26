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
import re
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
from typing_extensions import NotRequired, TypedDict

import db
import hudoc_query
from hudoc_query import _HUDOC_BAS_QUERY, BotskyddFel
from mcp_annotationer import CACHE_HINTAR, LASNING_EXTERN
from mcp_transport import starta

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

SERVER_VERSION = "3.0.0"

HUDOC_TIMEOUT         = int(os.getenv("HUDOC_TIMEOUT", "30"))
HUDOC_SOKRESULTAT_MAX = int(os.getenv("HUDOC_SOKRESULTAT_MAX", "50"))

# Query-expansion (valfritt) — aktiveras via QUERY_EXPANSION_ENABLED=true i .env.
# Stöder alla OpenAI-kompatibla endpoints (till exempel Anthropic, OpenAI, Ollama, LM Studio).
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
    """Svar från echr_search och echr_hamta_svenska_mal.

    kalla är "hudoc" eller "lokal_cache". Ett svar ur lokal cache bär också
    synkdatum och en anmarkning om vad det innebär.
    """
    kalla: str
    totalt_antal: int
    start: int
    antal_returnerade: int
    nasta_start: int | None
    expansion: list[str] | None
    resultat: list[Traff]
    synkdatum: NotRequired[str | None]
    anmarkning: NotRequired[str]


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
    då varför. metadata_kalla är "hudoc" eller "lokal_cache"; ur lokal cache
    bär svaret också synkdatum.
    """
    metadata: Traff
    metadata_kalla: str
    fulltext: str | None
    antal_tecken: int | None
    kalla: str | None
    tecken_totalt: int | None
    trunkerad: bool | None
    fortsatt_fran_tecken: int | None
    anmarkning: str | None
    synkdatum: NotRequired[str | None]

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
        "https://hudoc.echr.coe.int i stället för att försöka igen direkt. "
        "LOKAL RESERV: när HUDOC inte svarar besvaras sökningar utan fritext på "
        "svenska mål (respondent=SWE) eller importance=1, och ECLI-uppslag, ur "
        "lokalt synkade metadata. Sådana svar har kalla/metadata_kalla "
        "'lokal_cache' och synkdatum; nämn det för användaren. Fulltext levereras "
        "då bara för avgöranden som redan ligger i den lokala fulltextcachen."
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
    if not QUERY_EXPANSION_MODEL:
        log.warning("QUERY_EXPANSION_MODEL saknas i .env; frågeexpansionen hoppas över.")
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
            model=QUERY_EXPANSION_MODEL,
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

    soktermer = [t for t in (_rensa_sokterm(t) for t in soktermer) if t]
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


# Värden som sätts in i HUDOC:s frågespråk. Det saknar escape-mekanism, så
# fälten valideras mot sitt kända format i stället: ett citattecken eller en
# parentes i ett filtervärde skulle annars ändra frågans struktur.
_RE_RESPONDENT = re.compile(r"^[A-Z]{3}$")
_RE_SAMLING    = re.compile(r"^[A-Z0-9_]{2,40}$")
_RE_ECLI       = re.compile(r"^ECLI:CE:ECHR:\d{4}:[0-9A-Z.]{1,40}$")
_RE_ITEMID     = re.compile(r"^\d{3}-\d{1,12}(-\d{1,12})?$")


def _validera_respondent(respondent: str | None) -> str | None:
    if not respondent:
        return None
    varde = respondent.strip().upper()
    if not _RE_RESPONDENT.match(varde):
        raise ToolError(
            f"Ogiltig respondent {respondent!r}. Ange svarandestaten som "
            "trebokstavskod enligt ISO 3166-1 alpha-3, t.ex. SWE, DEU eller FRA."
        )
    return varde


def _validera_samling(samling: str | None) -> str | None:
    if not samling:
        return None
    varde = samling.strip().upper()
    if not _RE_SAMLING.match(varde):
        raise ToolError(
            f"Ogiltig samling {samling!r}. Exempel på giltiga värden: GRANDCHAMBER, "
            "CHAMBER, JUDGMENTS, DECISIONS, COMMUNICATEDCASES, CLIN."
        )
    return varde


def _validera_ecli(ecli: str) -> str:
    varde = (ecli or "").strip().upper()
    if not _RE_ECLI.match(varde):
        raise ToolError(
            f"Ogiltigt ECLI {ecli!r}. ECHR:s ECLI har formatet "
            "ECLI:CE:ECHR:ÅÅÅÅ:MMDDTYP######, t.ex. ECLI:CE:ECHR:1988:0324JUD001046583."
        )
    return varde


def _rensa_sokterm(term: str) -> str:
    """Tar bort tecken som bryter HUDOC:s frågesyntax ur en fritextterm."""
    return re.sub(r'["()\\]', " ", term).strip()


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


def _iso_datum(s) -> str | None:
    """HUDOC-datum som YYYY-MM-DD, eller None om det inte går att tolka."""
    return hudoc_query.iso_datum(s)


def _formattera_sokresultat(hudoc_rader: list[dict]) -> list[Traff]:
    """Konverterar HUDOC-rådata till ett rent svarsformat.

    datum           = domsdatum (judgementdate) på ISO-format YYYY-MM-DD
    publiceringsdatum = publiceringsdatum i HUDOC (kpdate) på ISO-format
    OBS: ar_fran/ar_till i sökverktygen filtrerar på publiceringsdatum (kpdate),
    inte på domsdatum.
    """
    t = hudoc_query.text_eller_none
    resultat: list[Traff] = []
    for rad in hudoc_rader or []:
        kol = (rad or {}).get("columns") or {}
        importance = hudoc_query.heltal_eller_none(kol.get("importance"))
        resultat.append({
            "itemid":            t(kol.get("itemid")),
            "appno":             t(kol.get("appno")),
            "datum":             _iso_datum(kol.get("judgementdate")),
            "publiceringsdatum": _iso_datum(kol.get("kpdate")),
            "respondent":        t(kol.get("respondent")),
            "ecli":              t(kol.get("ecli")),
            "samling":           t(kol.get("doctypebranch")),
            "importance":        str(importance) if importance is not None else t(kol.get("importance")),
            "artikel":           t(kol.get("article")),
            "slutsats":          t(kol.get("conclusion")),
            "sprak":             t(kol.get("languageisocode")),
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
    if db.hamta_avgorande(itemid) or not _RE_ITEMID.match(itemid):
        return
    try:
        svar = _hudoc_sok_live(f"{_HUDOC_BAS_QUERY} AND (itemid={itemid})", antal=1)
        rader = svar.get("results") or []
        if not rader:
            return
        kol = (rader[0] or {}).get("columns") or {}
        db.spara_avgorande({
            "itemid":            kol.get("itemid", itemid),
            "appno":             kol.get("appno", ""),
            "domsdatum":         _iso_datum(kol.get("judgementdate")),
            "publiceringsdatum": _iso_datum(kol.get("kpdate")),
            "svarandestat":      kol.get("respondent", ""),
            "ecli":              kol.get("ecli", ""),
            "samling":           kol.get("doctypebranch", ""),
            "importance":        hudoc_query.heltal_eller_none(kol.get("importance")),
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
# Lokala metadata som reserv när HUDOC inte svarar
#
# Synken (02_synka_metadata.py) håller tre urval kompletta: alla avgöranden
# mot Sverige, alla med importance=1 och alla Key cases. Bara frågor som
# ryms helt i ett sådant urval besvaras lokalt; allt annat skulle ge ett
# ofullständigt svar som ser komplett ut. Fritext kan inte besvaras lokalt,
# eftersom titlar och fulltext inte synkas.
# ---------------------------------------------------------------------------

def _orsak(fel: Exception) -> str:
    """Kort beskrivning av varför HUDOC inte kunde användas."""
    if isinstance(fel, hudoc_query.HastighetsgransFel):
        return "HUDOC begränsar just nu antalet anrop (HTTP 429)"
    if isinstance(fel, BotskyddFel):
        return "HUDOC blockerar just nu automatiserade anrop (Cloudflares botkontroll)"
    return "HUDOC gick inte att nå"


def _datumtext(v) -> str | None:
    """Datum från databasen (date eller ISO-text) som ISO-text."""
    return str(v)[:10] if v else None


def _traff_fran_db(rad: dict) -> Traff:
    """Konverterar en rad ur avgorande_cache till samma form som HUDOC-träffar."""
    t = hudoc_query.text_eller_none
    importance = hudoc_query.heltal_eller_none(rad.get("importance"))
    return {
        "itemid":            t(rad.get("itemid")),
        "appno":             t(rad.get("appno")),
        "datum":             _datumtext(rad.get("domsdatum")),
        "publiceringsdatum": _datumtext(rad.get("publiceringsdatum")),
        "respondent":        t(rad.get("svarandestat")),
        "ecli":              t(rad.get("ecli")),
        "samling":           t(rad.get("samling")),
        "importance":        str(importance) if importance is not None else None,
        "artikel":           t(rad.get("artikel")),
        "slutsats":          t(rad.get("slutsats")),
        "sprak":             t(rad.get("sprak")),
    }


def _har_artikel(artikelfalt: str | None, artikel: int) -> bool:
    """Motsvarar HUDOC:s article=N: artikeln eller en punkt i den, t.ex. 8-1."""
    if not artikelfalt:
        return False
    nr = str(artikel)
    return any(t == nr or t.startswith(f"{nr}-") for t in artikelfalt.split(";"))


def _lokal_anmarkning(orsak: str, status: dict) -> str:
    """Anmärkningen som följer med varje svar ur lokal cache."""
    synkdatum = status.get("synkdatum") or "okänt datum"
    senast = status.get("senast_publicerad") or "okänt"
    return (
        f"{orsak}. Svaret kommer därför ur lokal cache: metadata synkade från "
        f"HUDOC, senast {synkdatum} (senast publicerade avgörande i lokala data: "
        f"{senast}). Nyare avgöranden kan saknas. Fulltext kan inte hämtas från "
        "HUDOC just nu; echr_hamta_dom levererar bara avgöranden som redan finns "
        "i den lokala fulltextcachen. Sök direkt på https://hudoc.echr.coe.int "
        "för ett aktuellt svar."
    )


def _lokal_sokning(
    fel: Exception,
    fritext: str | None,
    respondent: str | None,
    artikel: int | None,
    importance: int | None,
    ar_fran: int | None,
    ar_till: int | None,
    samling: str | None,
    start: int,
    antal: int,
) -> Sokresultat | None:
    """Besvarar en sökning ur lokala metadata, eller None om det inte går."""
    svarandestat = respondent.upper() if respondent else None
    if fritext or samling:
        return None
    if svarandestat != "SWE" and importance != 1:
        return None

    rader = db.lista_lokala_avgoranden(svarandestat, importance, ar_fran, ar_till)
    if rader is None:
        return None
    if artikel is not None:
        rader = [r for r in rader if _har_artikel(r.get("artikel"), artikel)]

    status = db.lokal_synkstatus()
    totalt = len(rader)
    sida = rader[start:start + antal]
    log.info("Lokal sökning (%s): %d träffar", type(fel).__name__, totalt)
    return {
        "kalla": "lokal_cache",
        "totalt_antal": totalt,
        "start": start,
        "antal_returnerade": len(sida),
        "nasta_start": start + len(sida) if start + len(sida) < totalt else None,
        "expansion": None,
        "resultat": [_traff_fran_db(r) for r in sida],
        "synkdatum": status.get("synkdatum"),
        "anmarkning": (
            _lokal_anmarkning(_orsak(fel), status)
            + " Träffarna är ordnade efter publiceringsdatum, nyast först; "
            "HUDOC:s egen rangordning finns inte lokalt."
        ),
    }


def _lokal_ecli(fel: Exception, ecli: str) -> EcliSvar | None:
    """Besvarar ett ECLI-uppslag ur lokala metadata och fulltextcache."""
    rader = db.hamta_avgoranden_via_ecli(ecli)
    if not rader:
        return None

    # Flera språkversioner kan dela ECLI. Välj en vars fulltext redan är
    # cachad, annars den engelska, annars den första.
    fulltexter = {r["itemid"]: db.hamta_fulltext_fran_cache(r["itemid"]) for r in rader}
    rad = next((r for r in rader if fulltexter.get(r["itemid"])), None)
    rad = rad or next((r for r in rader if r.get("sprak") == "ENG"), rader[0])
    text = fulltexter.get(rad["itemid"])

    status = db.lokal_synkstatus()
    anmarkning = _lokal_anmarkning(_orsak(fel), status)
    svar: EcliSvar = {
        "metadata": _traff_fran_db(rad),
        "metadata_kalla": "lokal_cache",
        "fulltext": None,
        "antal_tecken": None,
        "kalla": None,
        "tecken_totalt": None,
        "trunkerad": None,
        "fortsatt_fran_tecken": None,
        "anmarkning": anmarkning,
        "synkdatum": status.get("synkdatum"),
    }
    if text:
        u = _skar_ut(text, ECHR_MAX_TECKEN, 0)
        svar.update({
            "fulltext": u["text"],
            "antal_tecken": u["tecken_visade"],
            "kalla": "cache",
            "tecken_totalt": u["tecken_totalt"],
            "trunkerad": u["trunkerad"],
            "fortsatt_fran_tecken": u["fortsatt_fran_tecken"],
        })
    else:
        svar["anmarkning"] = (
            f"{anmarkning} Fulltexten till {rad['itemid']} finns inte i den lokala cachen."
        )
    return svar


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
    respondent = _validera_respondent(respondent)
    samling = _validera_samling(samling)

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
    except (BotskyddFel, requests.RequestException) as e:
        log.warning("echr_search: HUDOC otillgänglig: %s", e)
        lokalt = _lokal_sokning(
            e, fritextsokning, respondent, artikel, importance,
            ar_fran, ar_till, samling, start, antal,
        )
        if lokalt is not None:
            return lokalt
        tips = (
            " Utan fritext och samling kan svenska mål (respondent=SWE) och "
            "avgöranden med importance=1 besvaras ur lokala metadata."
        )
        if isinstance(e, BotskyddFel):
            raise ToolError(f"{e}{tips}") from e
        raise ToolError(f"{_hudoc_fel(e)}{tips}") from e

    rader = svar.get("results") or []
    totalt = hudoc_query.heltal_eller_none(svar.get("resultcount")) or 0

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
    ecli = _validera_ecli(ecli)

    # Sök upp itemid via ECLI
    try:
        query = f'{_HUDOC_BAS_QUERY} AND (ecli="{ecli}")'
        svar = _hudoc_sok_live(query, antal=1)
        rader = svar.get("results", [])
    except (BotskyddFel, requests.RequestException) as e:
        log.warning("echr_hitta_via_ecli: HUDOC otillgänglig: %s", e)
        lokalt = _lokal_ecli(e, ecli)
        if lokalt is not None:
            return lokalt
        grund = str(e) if isinstance(e, BotskyddFel) else str(_hudoc_fel(e))
        raise ToolError(f"{grund} ECLI {ecli!r} finns inte heller i de lokala metadata.") from e

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
            "metadata_kalla": "hudoc",
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
        "metadata_kalla": "hudoc",
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
