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

# Document-shape version. generate_documents() stamps
# DERIVED/documents_version.json with this; build-index refuses to embed a
# documents.json whose stored version differs (otherwise it would silently
# serve stale docs — the classic build-index-vs-build-documents trap).
# Bump whenever document generation changes shape (new metadata keys, table
# records, chunking) so a stale persist is surfaced, not re-embedded.
DOCUMENTS_VERSION = 15

DOCUMENTS_VERSION_FILE = DERIVED_DIR / "documents_version.json"

EMBEDDING_DIM = 384

# Max context chars fed to the LLM (output is separate). 80000 is the
# golden-validated config: the then-current production winner (gemma-4-12B-it-qat) measured a
# 7-facts-missing baseline at this value. A tighter worst-density-safe 54k
# cap did NOT help golden (8 missing — wide general/lookup contexts lost
# facts; a later 80k run scored 10, all within run-to-run LLM variance), so
# the golden-validated value wins and extraction is NOT traded for a
# theoretical worst-density guarantee. rag/pipeline.py (`_fit_context`) hard-
# caps prompt context to this budget, so retrieval + sibling expansion can
# never silently push past it (the reachable overflow). Residual caveat:
# ~80k chars of the densest content (~2.4 chars/token, measured worst in
# chunking.py) could approach the 32k window's token edge; never observed in
# goldens. Entity/dossier path caps at CONTEXT_BUDGET (per-entity split for
# multi-entity); general queries feed whatever retrieval returns, capped by
# the guard.
CONTEXT_BUDGET = 80000
