import contextlib
import json
import math
import os
import pickle
import re
import time
from collections import Counter
from pathlib import Path

from pgrag.buildlock import corpus_lock

CACHE_VERSION = 1
DEFAULT_INDEX_PATH = "data/documents.json"
DEFAULT_PICKLE_PATH = "data/bm25_index.pkl"

# How long the cache publish retries a win32 sharing violation before giving up
# (see `_publish_pickle`): 30 x 0.1 s covers a peer's in-flight `pickle.load` of
# the index without stalling a build behind a genuinely wedged reader.
PUBLISH_RETRIES = 30
PUBLISH_RETRY_S = 0.1

# A lazy rebuild happens inside a request (the loop's corpus_search), so it
# polls for a peer's build step this long before raising BuildLocked instead of
# rebuilding over it.
LAZY_REBUILD_WAIT_S = 30.0

# Query-side stopwords (the index itself keeps every token, so the persisted
# pickle stays valid). A long natural-language question otherwise ranks docs
# that merely repeat function words above the doc holding the answer: common
# terms carry a small-but-nonzero idf, and a doc stuffed with 'the'/'does'/'it'
# accumulates more of them than a focused doc earns from its few rare terms.
# Measured 2026-10-15 — the raw `dungcrafting-poop-ability-and-rewards`
# question ranked `wiki_Citan Forum Posts…` and `effect_effect_7028` above
# `skillprofile_Pooping_chunk_0`; without function words the profile (and
# `skill_Pooping`) enter the top 5. It also RAISES docs within the seed rather
# than newly seeding them for other long-question goldens (ratkin's
# `creature_Ratkin_Root-Tender` 7 → 1, strom's `wiki_Strom Farblood_Favor`
# 3 → 1) — those were already in the k=12 seed.
# NOT a free win: the drop removes the credit long prose docs used to earn from
# function words, so some seeds cover FEWER facts than before — measured over
# the 61 golden questions, a strict multi-word-fact substring check on the
# k=12 seed loses 13 fact-group hits and gains 15 (worst: meditation-synergy
# 15 → 8; animal-handling-trainer-bands 2 → 0). Those cases pass because the
# model's own tool calls carry the facts, and the full gate is the arbiter
# (59/61 → 61/61 on 2026-10-15), not the seed.
# Bare words only: `_tokenize` is `\w+`, so "don't" tokenizes to don + t and an
# entry like "don't" would never match. `off`/`only`/`under` are DELIBERATELY
# absent: hyphenated corpus vocabulary tokenizes to them (Off-Hand, Off-Guard,
# '(Fae Only)', 'Caves Under Serbule') and dropping them inverts those queries
# (measured: the Off-Hand bead question then ranks a Main-Hand row #1).
# Contract: tests/test_bm25.py.
_QUERY_STOPWORDS = frozenset((
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any",
    "are", "as", "at", "be", "because", "been", "before", "being", "below", "between",
    "both", "but", "by", "can", "cannot", "could", "did", "do", "does", "doing", "down",
    "during", "each", "few", "for", "from", "further", "had", "has", "have", "having", "he",
    "her", "here", "hers", "herself", "him", "himself", "his", "how", "i", "if", "in",
    "into", "is", "it", "its", "itself", "just", "me", "more", "most", "must", "my",
    "myself", "no", "nor", "not", "of", "on", "once", "or", "other", "ought",
    "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should", "so", "some",
    "such", "than", "that", "the", "their", "theirs", "them", "themselves", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "until", "up", "very",
    "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom", "why",
    "with", "would", "you", "your", "yours", "yourself", "yourselves",
))


class BM25:
    def __init__(self, k1=1.2, b=0.75):
        self.k1 = k1
        self.b = b
        self.doc_count = 0
        self.avgdl = 0.0
        self.doc_lengths = []
        self.documents = []
        self.postings = {}
        self.df = Counter()

    def index(self, documents):
        self.documents = list(documents)
        self.doc_count = len(self.documents)
        total_length = 0
        self.doc_lengths = []
        self.postings = {}
        self.df = Counter()

        for doc_idx, doc in enumerate(self.documents):
            tokens = self._tokenize(doc)
            seen = set()
            for t in tokens:
                if t not in seen:
                    seen.add(t)
                    self.postings.setdefault(t, {})[doc_idx] = 1
                    self.df[t] += 1
                else:
                    self.postings[t][doc_idx] += 1
            self.doc_lengths.append(len(tokens))
            total_length += len(tokens)

        self.avgdl = total_length / self.doc_count if self.doc_count else 0.0

    def _tokenize(self, text):
        return re.findall(r"\w+", text.lower())

    def _idf(self, term):
        n = len(self.postings.get(term, {}))
        return math.log((self.doc_count - n + 0.5) / (n + 0.5) + 1.0)

    def search(self, query, k=10):
        query_terms = self._tokenize(query)
        if not query_terms or not self.doc_count:
            return [], []
        # Function words carry no signal for lexical ranking (see
        # _QUERY_STOPWORDS). A query made ONLY of them ("what is it") keeps them
        # rather than returning nothing.
        content_terms = [t for t in query_terms if t not in _QUERY_STOPWORDS]
        if content_terms:
            query_terms = content_terms

        tf = Counter(query_terms)
        scores = {}
        # Sparse pass over precomputed postings: only docs that contain a
        # query term are scored, so no per-query 98 MB re-tokenization.
        for term in query_terms:
            idf = self._idf(term)
            qf = tf[term]
            for doc_idx, term_freq in self.postings.get(term, {}).items():
                denominator = term_freq + self.k1 * (
                    1 - self.b + self.b * self.doc_lengths[doc_idx] / self.avgdl
                )
                contribution = idf * qf * term_freq * (self.k1 + 1) / denominator
                scores[doc_idx] = scores.get(doc_idx, 0.0) + contribution

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top = ranked[:k]
        return [idx for idx, _ in top], [score for _, score in top]


def _documents_mtime(source_path):
    try:
        return os.path.getmtime(source_path)
    except OSError:
        return None


def _publish_pickle(tmp: str, path: str) -> None:
    """Rename `tmp` over `path`, retrying a win32 sharing violation.

    `os.replace` onto a file another process holds open fails on Windows with
    WinError 32 — a CPython `open` shares read/write but not delete — and the
    overlap is normal rather than exotic: `load_bm25_index` reads the cache
    *before* it takes the lock, so any `corpus_search` in flight pins the
    pickle for the whole multi-second `pickle.load` of a ~100 MB index. A
    single-shot rename there turns the persist into a silent no-op, so retry a
    bounded time; the reader is on its way out. The final failure is raised
    (not swallowed) so a caller that promised to write the cache can say so.
    """
    last: OSError | None = None
    for _ in range(PUBLISH_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # win32: the target is open by a reader
            last = exc
            time.sleep(PUBLISH_RETRY_S)
    raise last  # type: ignore[misc]  # set by the loop above


def save_bm25_index(model, documents, path=DEFAULT_PICKLE_PATH, source_path=DEFAULT_INDEX_PATH):
    """Persist an indexed BM25 + the doc store to `path`, keyed by the
    documents.json mtime so `load_bm25_index` can detect staleness.

    Written to a sibling temp file and renamed: the cache-hit read in
    `load_bm25_index` happens *before* the build lock, so an in-place write
    could hand a concurrent reader a torn pickle. The rename itself retries a
    reader-held target (`_publish_pickle`).
    """
    payload = {
        "__version": CACHE_VERSION,
        "documents_mtime": _documents_mtime(source_path),
        "k1": model.k1,
        "b": model.b,
        "doc_count": model.doc_count,
        "avgdl": model.avgdl,
        "doc_lengths": model.doc_lengths,
        "postings": model.postings,
        "documents": documents,
    }
    # Unique per *call*, not just per pid: two saves in one process (a lazy
    # rebuild racing an explicit build) must not share a temp name, or the
    # second's dump/unlink would clobber the first mid-write and a reader's
    # `os.replace` could publish a half-written pickle.
    tmp = f"{path}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        with open(tmp, "wb") as f:
            pickle.dump(payload, f)
        _publish_pickle(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def load_bm25_index(
    path=DEFAULT_INDEX_PATH,
    pkl_path=DEFAULT_PICKLE_PATH,
    lock_wait: float | None = None,
    strict_persist: bool = False,
):
    """Return (BM25, docs).

    Serves the cached index from `pkl_path` while `documents.json` is
    unchanged (mtime-keyed); otherwise rebuilds and rewrites the cache.

    `lock_wait` is how long the rebuild may wait for a peer's corpus build to
    finish before raising `BuildLocked`: the lazy in-request default
    (`LAZY_REBUILD_WAIT_S`) is wrong for an explicit `python -m pgrag.rag.bm25`
    build, which should fail fast like every other corpus writer.

    `strict_persist` re-raises a failed cache write instead of swallowing it.
    A lazy in-request rebuild treats the write as best-effort — the answer must
    not fail because the cache could not be rewritten — but the explicit
    pre-warm exists *only* to write that cache, so it must not report success
    when the publish did not land.
    """
    mtime = _documents_mtime(path)
    if os.path.exists(pkl_path):
        try:
            with open(pkl_path, "rb") as f:
                state = pickle.load(f)
            if (
                state.get("__version") == CACHE_VERSION
                and state.get("documents_mtime") == mtime
                and state.get("doc_count", 0) > 0
                and isinstance(state.get("documents"), list)
            ):
                model = BM25(k1=state["k1"], b=state["b"])
                model.doc_count = state["doc_count"]
                model.avgdl = state["avgdl"]
                model.doc_lengths = state["doc_lengths"]
                model.postings = state["postings"]
                model.df = Counter({term: len(p) for term, p in state["postings"].items()})
                return model, state["documents"]
        except (
            OSError,
            EOFError,
            pickle.UnpicklingError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            pass  # stale/corrupt cache -> rebuild below

    # A rebuild rewrites the persisted index, i.e. it is a corpus write: hold
    # the lock that lives beside the cache (data/.omp-build.lock for the
    # defaults) so two instances never rebuild over each other. The wait
    # differs by caller: a lazy rebuild inside a request polls briefly for a
    # peer's step and then raises (`corpus_search` turns that into an error
    # string), while the explicit `__main__` build fails fast like the other
    # corpus writers.
    with corpus_lock(
        Path(pkl_path).resolve().parent,
        wait=LAZY_REBUILD_WAIT_S if lock_wait is None else lock_wait,
    ):
        with open(path, encoding="utf-8") as f:
            docs = json.load(f)
        model = BM25()
        model.index([doc["text"] for doc in docs])
        try:
            dirname = os.path.dirname(pkl_path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            save_bm25_index(model, docs, pkl_path, path)
        except OSError:
            if strict_persist:
                raise
            # cache write is best-effort here: the rebuild's answer stands
    return model, docs


if __name__ == "__main__":
    # Pre-warm/build the BM25 index. load_bm25_index() rebuilds in-memory
    # AND persists when documents.json's mtime changed — otherwise the first
    # hybrid retrieve() pays a multi-minute sync rebuild inline. Run after
    # build-documents to avoid that first-query spike.
    import sys

    sys.stdout.reconfigure(line_buffering=True)
    # lock_wait=0: an explicit build fails fast on a peer's lock, like the
    # other corpus writers — waiting is only right for the in-request rebuild.
    # strict_persist=True: this command's *job* is the cache write, so a publish
    # that did not land must fail the command instead of printing success.
    load_bm25_index(lock_wait=0.0, strict_persist=True)
    print("BM25 index ready (rebuilt if documents.json changed)")
