import json
import re

from pgrag.config import WIKI_DIR

# Matches downloader filenames: {safe_title}_{8-hex-sha256}.txt
_HASHED_FILE_RE = re.compile(r"^(.*)_[0-9a-f]{8}\.txt$")


def _load_title_map():
    """Map filename -> real wiki page title from the downloader's meta.

    Falls back to {} when meta is missing or unreadable (partial/stale runs).
    """
    try:
        meta = json.loads((WIKI_DIR / ".meta.json").read_text(encoding="utf-8"))
        pages = meta.get("pages", {})
        return {
            info.get("filename"): title for title, info in pages.items() if info.get("filename")
        }
    except (OSError, ValueError, AttributeError):
        return {}


def load_wiki(db):
    db.wiki = {}
    db.wiki_mtimes = {}
    title_map = _load_title_map()
    seen = set()

    for file in sorted(WIKI_DIR.glob("*.txt")):
        match = _HASHED_FILE_RE.match(file.name)
        if not match:
            continue  # legacy un-hashed files from older downloader schemes

        page_name = title_map.get(file.name, match.group(1))
        if page_name in seen:
            counter = 2
            while f"{page_name}_{counter}" in seen:
                counter += 1
            page_name = f"{page_name}_{counter}"
        seen.add(page_name)

        db.wiki[page_name] = file.read_text(encoding="utf-8")
        db.wiki_mtimes[page_name] = file.stat().st_mtime

    print(f"Loaded {len(db.wiki)} wiki pages.")
