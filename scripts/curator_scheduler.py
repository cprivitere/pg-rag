"""Curator scheduler — run curator periodically, detect changes, rebuild the store.

Ordering note: this scheduler is curator -> store build -> state save, and is
the only path that produces `data/wiki/curated/*_curated.txt` before
`build_sqlstore.py` ingests them via `build_curated_documents`. The
`mise sync-*` tasks are source-download(s) -> store build and `sql-store` is
store build only; neither runs the curator.
"""

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

    # Rebuild the store so new curated docs reach the generated corpus (the
    # store build runs the document builders in-process; SQLite/WAL is the
    # single-writer property, so no lock dance and no state-is-below ordering).
    # `mise sync-*` never runs this curator step — this scheduler is the only
    # curator -> store build path.
    print("Rebuilding the store...")
    store_result = subprocess.run(
        [sys.executable, "scripts/build_sqlstore.py"], capture_output=True, text=True
    )

    if store_result.returncode != 0:
        print(f"Store rebuild failed: {store_result.stderr}")
        return

    # Update state
    state["last_run"] = datetime.now().isoformat()
    state["files_hash"] = current_hash
    save_state(state)

    print("Curator run complete.")


if __name__ == "__main__":
    run_curator_with_scheduler()
