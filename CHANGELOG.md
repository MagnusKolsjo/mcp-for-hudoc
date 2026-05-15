# Ändringslogg

Alla viktiga ändringar dokumenteras här. Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
och versionshanteringen följer [Semantic Versioning](https://semver.org/).

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
