# mcp-for-hudoc

MCP-server som ger AI-verktyg åtkomst till Europadomstolens för mänskliga rättigheters (ECHR) rättspraxis via HUDOC-databasen.

Europakonventionen är grundlagsskyddad i Sverige (RF 2:19) och Europadomstolens avgöranden är en primär rättskälla för svenska domstolar. HD och HFD hänvisar regelmässigt till HUDOC-domar.

## Verktyg

| Verktyg | Beskrivning |
|---|---|
| `echr_search` | Söker i HUDOC med valfria filter (respondent, artikel, importance, datum, samling) |
| `echr_hamta_dom` | Hämtar fulltext för ett avgörande via itemid |
| `echr_hamta_svenska_mal` | Bekvämlighetsverktyg: söker automatiskt med respondent=SWE |
| `echr_hitta_via_ecli` | Hämtar metadata och fulltext för ett avgörande via ECLI |

## Datakälla

HUDOC är Europadomstolens officiella databas. Ingen autentisering krävs, men
sedan 2026-09-15 stoppar HUDOC automatiserade anrop, och live-hämtning kräver
att installationen vitlistas. Se [Botskydd](#botskydd-och-lokal-reserv) och
[Vitlistning](#vitlistning) nedan.

- Portal: https://hudoc.echr.coe.int/
- ~230 000 dokument (domar, beslut, kommunicerade mål m.m.)
- ~173 mål med Sverige som svarandestat och hög importance

## Krav

- Python 3.11+
- MCP Python SDK 2.x (`mcp>=2.0,<3`)
- PostgreSQL eller SQLite (välj efter behov)
- Nätverksåtkomst till hudoc.echr.coe.int

## Installation

```bash
git clone https://github.com/MagnusKolsjo/mcp-for-hudoc.git
cd mcp-for-hudoc

python3 -m venv .venv --without-pip
.venv/bin/python3 -m ensurepip
.venv/bin/python3 -m pip install -r requirements.txt

cp config.example.env .env
# Redigera .env med din DATABASE_URL
```

## Försynkning av metadata

Synkar metadata för ~6 500 avgöranden via tre filter:
- Alla importance=1-avgöranden (~2 800 st, alla stater)
- Alla Case Reports / Key cases (~3 100 st, doctypebranch=REPORTS)
- Alla avgöranden med Sverige som svarandestat (~540 st, alla nivåer)

```bash
.venv/bin/python3 02_synka_metadata.py
```

Skriptet är tillståndsbaserat — kör det igen för att hämta nya avgöranden sedan senaste synkdatum.

## Konfiguration i MCP-klient

Lägg till i din MCP-klients konfiguration:

```json
"echr-hudoc": {
  "command": "/absolut/sökväg/till/.venv/bin/python3",
  "args": ["/absolut/sökväg/till/mcp_server.py"],
  "cwd": "/absolut/sökväg/till/mcp-for-hudoc"
}
```


## HTTP-transport

Standard är stdio: MCP-klienten startar processen själv. För delad drift bakom
en reverse proxy kan servern i stället lyssna på HTTP (Streamable HTTP):

```bash
MCP_TRANSPORT=http MCP_API_KEY=<nyckel> .venv/bin/python3 mcp_server.py
```

Servern lyssnar då på `http://MCP_HOST:MCP_PORT/mcp` (standard `127.0.0.1:8011`)
och kräver `Authorization: Bearer <MCP_API_KEY>` på varje anrop: 401 utan
header, 403 med fel nyckel. Utan `MCP_API_KEY` startar servern inte i
http-läget (exitkod 2).

## Botskydd och lokal reserv

HUDOC ligger bakom Cloudflare och svarar sedan 2026-09-15 på alla
automatiserade anrop, både sökning och fulltext, med en botkontroll ("Just a
moment…", HTTP 403 med `cf-mitigated: challenge`) i stället för data. Det är
källans val att stoppa automatiserade anrop, och servern försöker inte ta sig
förbi kontrollen. Utan vitlistning fungerar därför bara den lokala reserven
nedan.

När det händer:

- Verktygen svarar med ett fel (`isError`) som säger att HUDOC blockerar
  automatiserade anrop, att det inte är ett fel i frågan, och att sökningen
  kan göras direkt på https://hudoc.echr.coe.int.
- Servern avstår från nya anrop till HUDOC under
  `HUDOC_BOTSKYDD_PAUS_MINUTER` (standard 10), eftersom upprepade försök bara
  förlänger blockeringen.
- **Lokala metadata används som reserv** där de räcker för ett komplett svar:
  sökningar utan fritext och samling på svenska mål (`respondent=SWE`, även
  via `echr_hamta_svenska_mal`) eller på `importance=1`, samt ECLI-uppslag.
  Svaret har då `kalla`/`metadata_kalla` = `lokal_cache`, `synkdatum` och en
  `anmarkning` om att svaret kommer ur lokal cache och att fulltext inte kan
  hämtas från HUDOC just nu.
- Fulltext som redan ligger i den lokala fulltextcachen levereras som vanligt.
- Synkskriptet avbryts vid första botkontrollen och flyttar inte fram sin
  checkpoint, så att nästa körning tar igen det som missades.

User-Agent i alla anrop, från både servern och synkskriptet, sätts med
`HUDOC_USER_AGENT` i `.env`. Standard är projektets egen identifierare med
länk till repot; en egen installation bör gärna ange en egen identifierare
med kontaktadress.

### Vitlistning

För att sökning, fulltext och den dagliga synken ska fungera igen måste
Europadomstolen vitlista installationen:

1. Kontakta Europadomstolen via https://www.echr.coe.int/hudoc-database och
   beskriv användningen: ett ideellt verktyg för rättsinformation, få och
   glesa anrop, vilken User-Agent och helst vilken IP-adress anropen kommer
   från.
2. Ange den vitlistade identifieraren i `.env`:

   ```
   HUDOC_USER_AGENT=<projektnamn>/<version> (+<url>; <kontaktadress>)
   ```

3. Kör en full synk när anropen går igenom, eftersom den lokala metadatan
   annars saknar allt som publicerats sedan blockeringen började:
   `python3 02_synka_metadata.py --force-full`.

## Svarsstorlek och trunkering

MCP-protokollet har en övre storleksgräns per svar. Det största cachade avgörandet är **358 097 tecken**.
`echr_hamta_dom` tar därför två parametrar:

| Parameter | Innebörd |
|---|---|
| `max_tecken` | Teckentak för texten. Standard 60 000 tecken; `0` ger hela texten som ett uttryckligt val. |
| `fran_tecken` | Börja vid denna teckenposition — för att läsa vidare där ett kapat svar slutade. |

Ett kapat svar säger alltid ifrån med fälten `trunkerad`, `tecken_totalt` och `fortsatt_fran_tecken`. Kapningen sker på ordgräns, aldrig mitt i
ett ord.

**Vid ordagranna citat:** citera aldrig ur ett svar som är markerat som kapat.
Läs vidare med `fran_tecken` tills hela passagen är hämtad. Standardvärdet kan
sättas i `.env` med `ECHR_MAX_TECKEN`.

## Licens

AGPLv3 — se LICENSE. HUDOC-innehållet tillhör Europarådet och Europadomstolen.
