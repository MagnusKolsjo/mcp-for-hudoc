# Ändringslogg

Alla viktiga ändringar dokumenteras här. Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
och versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Dokumentation
- README beskriver att HUDOC sedan 2026-09-15 stoppar alla automatiserade anrop och att live-hämtning kräver vitlistning, med instruktion för hur det begärs och följs upp.

### Ändrat

- **Brytande:** kräver MCP Python SDK 2.x (`mcp>=2.0,<3`). Servern bygger på
  `MCPServer`, och transporten startas via `mcp_transport.starta()`.
- **Brytande:** http-läget kräver `MCP_API_KEY` och startar inte utan den
  (exitkod 2). Tidigare startade servern utan autentisering om nyckeln
  saknades. Fel nyckel ger nu 403 i stället för 401.
- **Brytande:** förväntade fel (botskydd, HTTP-fel, okänt itemid eller ECLI)
  kastas som verktygsfel (`isError`) med svenskt meddelande i stället för att
  returneras som `{"fel": ...}`.
- **Brytande:** `echr_hitta_via_ecli` har inte längre fältet `fel`. Kan
  fulltexten inte hämtas returneras metadata ändå, med skälet i det nya fältet
  `anmarkning`.
- Alla anrop mot HUDOC går över HTTPS. Sökningen gick tidigare över HTTP.
- HTTP-sessionen skapas på ett ställe (`hudoc_query.skapa_session()`) och
  delas av servern och synkskriptet.
- Alla verktyg har titel, annotationer och typade svar (`outputSchema` och
  `structuredContent`). Sökverktygen bär fältet `kalla`
  (`hudoc` eller `lokal_cache`), `echr_hitta_via_ecli` fältet `metadata_kalla`.
- Metadatafält i träffar normaliseras: tal, listor och text från HUDOC blir
  text, `importance` alltid heltal som text. Fält som saknas blir `null` i
  stället för tom sträng.
- `respondent`, `samling` och `ecli` valideras mot sitt format innan de sätts
  in i HUDOC-frågan; ett ogiltigt värde ger ett fel som visar rätt format.
  Citattecken och parenteser rensas ur fritexttermer.

### Tillagt

- `HUDOC_USER_AGENT`: User-Agent för alla anrop mot HUDOC, med projektets egen
  identifierare som standard.
- Igenkänning av Cloudflares botkontroll hos HUDOC (HTTP 403/503 med
  `cf-mitigated` eller utmaningssidan). Verktygen svarar med ett begripligt
  fel, och servern avstår från nya anrop under `HUDOC_BOTSKYDD_PAUS_MINUTER`
  (standard 10).
- Igenkänning av HTTP 429 från HUDOC: begripligt fel och paus enligt
  `Retry-After` (sekunder eller HTTP-datum), högst en timme.
- Lokala metadata som reserv när HUDOC inte svarar: sökning utan fritext på
  `respondent=SWE` eller `importance=1`, samt ECLI-uppslag. Svaret säger att
  det kommer ur lokal cache och anger synkdatum.
- `echr_hitta_via_ecli` bär `tecken_totalt`, `trunkerad` och
  `fortsatt_fran_tecken`, så att en kapad fulltext inte ser komplett ut.

### Rättat

- Synkskriptet flyttade fram `senaste_synk_datum` även när alla hämtningar
  misslyckats, och räknade rader som sparade även när databasen avvisat dem.
  Checkpointen flyttas nu bara fram när alla filter hämtats och alla rader
  sparats, och skriptet avslutar med kod 1 vid fel.
- Synkskriptet räknade en sidhämtning som slutade före `resultcount` som
  lyckad. Den räknas nu som fel, och checkpointen står kvar.
- Datum som inte går att tolka sparades som rå sträng, vilket fick Postgres
  att avvisa hela raden. De sparas nu som `NULL` med en varning i loggen.
- Ett ogiltigt `HUDOC_BOTSKYDD_PAUS_MINUTER` kraschade starten. Standardvärdet
  används nu, med en varning.
- Omdirigeringen av fil-deskriptor 1 under HTTP-anrop är borttagen. Med
  verktyg på arbetstrådar kunde den skicka andra anrops protokollsvar till
  loggfilen.

### Borttaget

- Den egna Starlette-appen med Bearer-middleware (ersatt av `mcp_transport.py`).

---

## [2.1.0] — 2026-08-10

### Tillagt

- **`max_tecken` och `fran_tecken` i `echr_hamta_dom`**, med standardtaket
  `ECHR_MAX_TECKEN` (60 000 tecken, konfigurerbart i `.env`). Det största cachade
  avgörandet är **358 097 tecken**; verktyget returnerade hela domtexten utan
  möjlighet att begränsa. Kapade svar bär `trunkerad`, `tecken_totalt` och
  `fortsatt_fran_tecken`, och kapas på ordgräns.
- Taket tillämpas på **båda kodvägarna** — cacheträff och live-hämtning från HUDOC —
  så att svarsstrukturen är densamma oavsett var texten kom ifrån.

### Bakgrund

Genomför projektets svarskontrakt (`00-las-forst.md` → "Svarskontraktet — storlek,
trunkering, adressering och sökning"). Additiva parametrar och fält; inga brytande
ändringar och inga schemaändringar. Cachen och databasen lagrar fortfarande hela
texten — trunkeringen gäller bara svaret till anroparen, så sökning och indexering
påverkas inte.

---

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
