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

HUDOC är Europadomstolens officiella databas. Åtkomsten är öppen — ingen autentisering krävs.

- Portal: https://hudoc.echr.coe.int/
- ~230 000 dokument (domar, beslut, kommunicerade mål m.m.)
- ~173 mål med Sverige som svarandestat och hög importance

## Krav

- Python 3.11+
- PostgreSQL eller SQLite (välj efter behov)
- Nätverksåtkomst till hudoc.echr.coe.int

## Installation

```bash
git clone https://github.com/MagnusKolsjo/mcp-for-hudoc.git
cd mcp-for-hudoc

python3 -m venv .venv --without-pip
.venv/bin/python3 -m ensurepip
.venv/bin/python3 -m pip install requests beautifulsoup4 psycopg2-binary python-dotenv "mcp[cli]"

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
