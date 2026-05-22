# Ändringslogg

Alla viktiga ändringar dokumenteras här. Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
och versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [2.0.1] — 2026-05-22

### Fixat
- `PYTHON_SOKVAG`-variabeln i `config.example.env` och `02_synka_metadata.py` hade ett icke-ASCII-tecken (`Ä`) i variabelnamnet — ersatt med rent ASCII.
- Standardvärdet för `PYTHON_SOKVAG` i `02_synka_metadata.py` pekar nu korrekt på `.venv/bin/python3` relativt skriptmappen (oförändrat för fristående installation). Variabeln måste sättas i `.env` om venv-mappen avviker från standarden.
- `config.example.env`: delad-venv-varianten (`../.venv/bin/python3`) dokumenteras nu som kommenterad alternativraden.

## [2.0.0] — 2026-05-22

### Fixat
- **Bugg:** Stavfel `domsatum` → `domsdatum` i `_konvertera_rad()` i `02_synka_metadata.py` — domsdatum sparades aldrig i DB vid synk, vilket gav NULL i alla befintliga poster. Kör `--force-full` för att backfilla.
- **Bugg:** `samling`-parametern gav 0 träffar för GRANDCHAMBER, DECISIONS och JUDGMENTS — filtret använde `doctypebranch=VÄRDE` (fel fält) istället för `documentcollectionid2:"VÄRDE"`.
- **Bugg:** `02_synka_metadata.py` kraschade på frisk klon (`FileNotFoundError`) — `logs/`-mappen skapades efter `logging.basicConfig`. Ordning korrigerad.
- `echr_hamta_dom` returnerar nu `sprak`-fältet konsekvent oavsett cache-träff eller HUDOC-hämtning.
- `datum`-fältet i `echr_search` och `echr_hamta_svenska_mal` normaliseras nu till ISO-format (YYYY-MM-DD) i stället för råformat DD/MM/YYYY HH:MM:SS.

### Tillagt
- `publiceringsdatum`-fält (kpdate, ISO-format) i sökresultat — tydliggör att `ar_fran`/`ar_till` filtrerar på publiceringsdatum, inte domsdatum.
- `hudoc_query.py`: gemensam modul med `_HUDOC_BAS_QUERY`, `SELECT_FALT` och `RANKING_MODEL_ID` — eliminerar drift mellan `mcp_server.py` och `02_synka_metadata.py`.
- Migration-block i `db.py:initiera_schema()` — framtida `ALTER TABLE`-satser läggs där.

### Borttaget
- `FULLTEXT_CACHE_DIR`-variabeln och mapp-skapandet i `mcp_server.py` — dead code, all fulltext cachas i DB.
- `ECHR_FULLTEXT_CACHE_DIR`-variabeln ur `config.example.env`.
- `fulltext_cache/` ur `.gitignore`.

### Ändrat
- `config.example.env`: platshållare använder nu `<VERSALER>`-format (`<MCP_API_NYCKEL>`, `<DB_LOSENORD>`, `<DB_ANVANDARE>`).
- `config.example.env` och `db.py`: "SQLite (fallback)" → neutrala formuleringar; PostgreSQL och SQLite beskrivs som symmetriska val.
- `README.md`: "pgvector (rekommenderas)" borttaget — koden använder GIN/TSVECTOR, inte pgvector-extensionen.
- `README.md`: synkbeskrivning uppdaterad med faktiska siffror (~6 500 poster) och alla tre filter.
- Bas-query i `02_synka_metadata.py` uppdaterad att exkludera `doctype=HFCOMOLD OR doctype=HECOMOLD` (samma som `mcp_server.py` — drift eliminerad via gemensam modul).

### Brytande
- `datum`-fältets format ändrat: DD/MM/YYYY HH:MM:SS → YYYY-MM-DD. Klienter som parsade det gamla formatet måste uppdateras.
- `publiceringsdatum`-fältet är nytt i sökresultaten (additivt, ej strikt brytande).

## [1.1.0] — 2026-05-15

### Tillagt
- `db.py`: SQLite-fallback med dubbelt backend-mönster (`_ar_postgres`, `_hamta_db`, `_cursor`, `_prefix`, `_ph`, `_now`) — väljs automatiskt via `DATABASE_URL` i `.env`
- `echr_search`: kommaseparerade OR-termer i `fritextsokning` (fraser bevaras intakta, ingen split på mellanslag)
- `echr_search`: valfri server-side query-expansion via LLM (`QUERY_EXPANSION_ENABLED=true` i `.env`) — returnerar `expansion`-fält i svar
- `expandera_fraga()`: flerspråkig begreppsexpansion via valfri OpenAI-kompatibel endpoint
- `prompts/expansion_prompt.txt`: promptfil anpassad för ECHR/MR-terminologi (EN + FR + SV)
- Nya `.env`-variabler: `QUERY_EXPANSION_ENABLED`, `QUERY_EXPANSION_BASE_URL`, `QUERY_EXPANSION_API_KEY`, `QUERY_EXPANSION_MODEL`, `QUERY_EXPANSION_PROMPT_FILE`

### Fixat
- Stavfel `domsatum` → `domsdatum` i `echr_hamta_dom()` — gav tysta fel vid metadata-lagring

## [1.0.0] — 2026-05-14

### Tillagt
- `echr_search`: sökning med filter för respondent, artikel, importance, datum och samling
- `echr_hamta_dom`: fulltexthämtning on-demand med lokal HTML-cache och språkfallback (SWE→ENG→FRE→GER→SPA→ITA)
- `echr_hamta_svenska_mal`: bekvämlighetsverktyg med respondent=SWE
- `echr_hitta_via_ecli`: ECLI-baserad uppslagning
- `02_synka_metadata.py`: tillståndsbaserad synk av importance=1, Key cases (REPORTS) och respondent=SWE — 6 451 poster
- PostgreSQL-schema `echr` med tabellerna `avgorande_cache`, `fulltext_cache` och `sync_status`
- Daglig launchd-synk (03:15, kör vid uppvakning)
- stdio- och HTTP-transport med Bearer-token-autentisering
