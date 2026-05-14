# echr-hudoc-mcp

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
- PostgreSQL med pgvector (rekommenderas) eller SQLite (fallback)
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

`02_synka_metadata.py` synkar metadata för tre grupper av avgöranden:

- Alla importance=1-mål (~2 800 st, alla stater)
- Alla Key cases / Case Reports (~3 100 st, `doctypebranch=REPORTS`)
- Alla mål med Sverige som svarandestat (~540 st, alla importance-nivåer)

Första körningen hämtar allt från 1959. Efterföljande körningar hämtar bara nya och uppdaterade poster sedan senaste synkdatum.

```bash
.venv/bin/python3 02_synka_metadata.py
```

Synka om allt från grunden:

```bash
.venv/bin/python3 02_synka_metadata.py --force-full
```

Installera daglig automatisk synk (launchd på macOS, cron på Linux):

```bash
.venv/bin/python3 02_synka_metadata.py --installera-schema
```

Tidpunkt och schemaläggare konfigureras via `CRON_SCHEMA` och `SCHEMALAGGARE` i `.env` (standard: 03:15 dagligen via launchd). På macOS med launchd körs missade jobb vid nästa uppvakning från viloläge.

## Konfiguration i MCP-klient

Lägg till i din MCP-klients konfiguration:

```json
"echr-hudoc": {
  "command": "/absolut/sökväg/till/.venv/bin/python3",
  "args": ["/absolut/sökväg/till/mcp_server.py"],
  "cwd": "/absolut/sökväg/till/echr-hudoc-mcp"
}
```

## Licens

AGPLv3 — se LICENSE. HUDOC-innehållet tillhör Europarådet och Europadomstolen.
