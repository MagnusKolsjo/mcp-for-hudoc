# Ändringslogg

Alla viktiga ändringar dokumenteras här. Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
och versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Tillagt
- `echr_search`: sökning med filter för respondent, artikel, importance, datum och samling
- `echr_hamta_dom`: fulltexthämtning on-demand med lokal HTML-cache
- `echr_hamta_svenska_mal`: bekvämlighetsverktyg med respondent=SWE
- `echr_hitta_via_ecli`: ECLI-baserad uppslagning
- `02_synka_metadata.py`: tillståndsbaserad synk av importance=1 + respondent=SWE
- PostgreSQL-schema `echr` med tabellerna `avgorande_cache` och `fulltext_cache`
- stdio- och HTTP-transport med Bearer-token-autentisering
