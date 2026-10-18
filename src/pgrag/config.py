from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"

CDN_DIR = DATA_DIR / "cdn"

WIKI_DIR = DATA_DIR / "wiki"

# Derived caches live outside the source dirs (data/cdn, data/wiki) so the
# raw download stays a read-only cache owned only by the download/verify
# scripts.
DERIVED_DIR = DATA_DIR / "derived"
WIKI_PARSED_CACHE = DERIVED_DIR / "wiki_parsed.json"
CURATED_DIR = WIKI_DIR / "curated"

# Document-shape version. The store's `documents` table stamps every row with
# this (`documents_version`); bump whenever document generation changes shape
# (new metadata keys, table records, chunking) so a reader can tell which
# generator rendered each row.
DOCUMENTS_VERSION = 23
