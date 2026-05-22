"""
hudoc_query.py — Gemensamma frågekonstanter för HUDOC-anrop.

Importeras av mcp_server.py och 02_synka_metadata.py för att undvika
att samma bas-query definieras på två ställen och riskerar att glida isär.
"""

# HUDOC kräver rankingModelId och HTTP (ej HTTPS) för results-endpointen.
# Utan denna parameter returnerar endpointen 404.
RANKING_MODEL_ID = "4180000c-8692-45ca-ad63-74bc4163871b"

# Bas-query med obligatorisk XRANK-struktur som HUDOC:s results-endpoint kräver.
# Utan contentsitename:ECHR och XRANK-strukturen returneras 404.
# OBS: Lägg INTE extra parenteser runt bas-queryn — XRANK-syntaxen bryts då.
# Extra filter läggs till med AND i slutet av kedjan (se respektive modul).
#
# Notering om skillnaden mot 02_synka_metadata.py (historisk drift nu avhjälpt):
#   mcp_server.py exkluderade: doctype=PR OR doctype=HFCOMOLD OR doctype=HECOMOLD
#   02_synka_metadata.py exkluderade enbart: doctype=PR
# Denna modul använder den mer kompletta exkluderingen från mcp_server.py.
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

# Metadatafält att begära i varje HUDOC-sökresultat.
SELECT_FALT = (
    "itemid,appno,judgementdate,kpdate,respondent,ecli,"
    "doctypebranch,importance,article,conclusion,languageisocode,typedescription"
)
