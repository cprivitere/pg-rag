"""Curator scheduler — run curator periodically, detect changes, rebuild corpora."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

STATE_FILE = Path("data/curator_state.json")


def load_state():
    """Load curator state from file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run": None, "files_hash": {}}


def save_state(state):
    """Save curator state to file."""
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_wiki_hash():
    """Get hash of wiki directory contents."""
    import hashlib

    wiki_dir = Path("data/wiki")
    if not wiki_dir.exists():
        return {}

    file_hash = {}
    for txt_file in wiki_dir.glob("*.txt"):
        try:
            content = txt_file.read_text(encoding="utf-8")
            file_hash[txt_file.name] = hashlib.md5(content.encode()).hexdigest()
        except Exception:
            continue

    return file_hash


def detect_changes(old_hash, new_hash):
    """Detect if wiki files have changed."""
    if not old_hash:
        return True

    # Check for new or modified files, then deleted files
    return any(
        name not in old_hash or old_hash[name] != hash_val for name, hash_val in new_hash.items()
    ) or any(name not in new_hash for name in old_hash)


def run_curator_with_scheduler():
    """Run curator with change detection and scheduling."""
    print("Checking curator schedule...")

    state = load_state()
    current_hash = get_wiki_hash()

    # Check if changes occurred
    changes_detected = detect_changes(state.get("files_hash", {}), current_hash)

    if not changes_detected:
        print("No wiki changes detected. Skipping curator run.")
        return

    print("Changes detected. Running curator...")

    # Run curator
    result = subprocess.run(
        [sys.executable, "-m", "scripts.curator"], capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"Curator failed: {result.stderr}")
        return

    print(result.stdout)

    # Rebuild documents so new curated docs reach documents.json (V25)
    print("Rebuilding documents.json...")
    docs_result = subprocess.run(
        [sys.executable, "-m", "pgrag.cli", "build-documents"], capture_output=True, text=True
    )

    if docs_result.returncode != 0:
        print(f"Documents rebuild failed: {docs_result.stderr}")
        return

    # Rebuild the derived corpora the query path reads, both from the
    # documents.json the previous step just wrote: the tool corpus that
    # corpus_search searches, then the full BM25 index (pre-warming it here
    # keeps the first query from paying the multi-minute inline rebuild).
    # Both are corpus writers and take the cross-instance build lock
    # themselves, so a concurrent instance serializes instead of interleaving.
    print("Rebuilding tool corpus...")
    tool_corpus_result = subprocess.run(
        [sys.executable, "-m", "scripts.build_tool_corpus"], capture_output=True, text=True
    )

    if tool_corpus_result.returncode != 0:
        print(f"Tool corpus rebuild failed: {tool_corpus_result.stderr}")
        return

    print("Rebuilding BM25 index...")
    bm25_result = subprocess.run(
        [sys.executable, "-m", "pgrag.rag.bm25"], capture_output=True, text=True
    )

    if bm25_result.returncode != 0:
        print(f"BM25 index rebuild failed: {bm25_result.stderr}")
        return

    # Update state
    state["last_run"] = datetime.now().isoformat()
    state["files_hash"] = current_hash
    save_state(state)

    print("Curator run complete.")


if __name__ == "__main__":
    run_curator_with_scheduler()
