import contextlib
import hashlib
import json
import os
import random
import sys
import time
from datetime import UTC, datetime

import requests

from pgrag.config import WIKI_DIR

with contextlib.suppress(AttributeError, ValueError):
    # not a real stream (e.g. captured by pytest)
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

META_FILE = WIKI_DIR / ".meta.json"

WIKI_HOST = "wiki.projectgorgon.com"
WIKI_API_URL = f"https://{WIKI_HOST}/api.php"

TARGET_CATEGORIES = [
    "Game updates",
    "Game Blogs",
    "NPCs",
    "Skills",
    "Abilities",
    "Beast Forms",
    "Arthropod",
    "Animal Handling Creatures",
    "Aberration",
    "Bear and Bugbear",
    "Anagoge Creatures",
    "Anagoge Records Facility Creatures",
    "Aktaari Cave Creatures",
    "Animal Nexus Creatures",
    "Amaluk Valley Cave Creatures",
    "Animal Town Creatures",
    "Augury",
    "Quests",
    "Dungeons",
]

RECURSIVE_CATEGORIES = {
    "Creatures": 2,
    "Items": 1,
}

# Pages with no [[Category:...]] membership are invisible to category-driven
# discovery (enumeration only walks Category: members). These titles are
# appended to the download queue directly. Verified uncategorized on the live
# wiki 2026-09-22; extend this list when a new uncategorized page matters.
WIKI_TITLE_EXTRAS = [
    "Peaceableness",
    "Community",
    "Special Behavior Badges",
    # Teleportation Machine codes: the only machine-code table on the wiki.
    # Uncategorized for fetch (sole category is Register of Lore, a 310-page
    # lorebook set nothing else needs), and Windy View Cave's embedded copy
    # predates the March 2025 code rotation (0003 = Prestonbule pad there).
    "Teleportation Machine",
]

MAX_RETRIES = 5
BATCH_SIZE = 50
BASE_DELAY = 0.5
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60

RESERVED_NAMES = {
    "CON",
    "NUL",
    "AUX",
    "PRN",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}
MAX_FILENAME_LEN = 150

USER_AGENT = "TwinkleofToesPersonalBackup/1.0 (sabin@figarocastle.org)"


def get_stable_filename(page_title: str) -> str:
    safe_title = "".join(c for c in page_title if c.isalnum() or c in (" ", "_", "-")).rstrip()
    safe_title = safe_title.replace(" ", "_")
    if not safe_title:
        safe_title = "page"
    stem = safe_title[:MAX_FILENAME_LEN].rstrip(" .")
    if stem.upper() in RESERVED_NAMES:
        stem += "_"
    digest = hashlib.sha256(page_title.encode("utf-8")).hexdigest()[:8]
    return f"{stem}_{digest}.txt"


def load_metadata() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_metadata(meta: dict) -> None:
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = META_FILE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(temp_path, META_FILE)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def api_call_with_retry(session: requests.Session, params: dict) -> dict:
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(
                WIKI_API_URL,
                params=params,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                error_code = data["error"].get("code", "")
                if error_code in {"ratelimited", "maxlag", "readonly"}:
                    retry_after = response.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after else 2**attempt + random.uniform(0, 1)
                    print(f"[{_ts()}]   API error {error_code}, retrying in {delay:.1f}s...")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"API error: {data['error']}")

            return data

        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                delay = (2**attempt) + random.uniform(0, 1)
                print(f"[{_ts()}]   Request failed, retrying in {delay:.1f}s... ({e})")
                time.sleep(delay)
            else:
                raise

    raise RuntimeError("Max retries exceeded")


def enumerate_category_pages(session: requests.Session, category_name: str) -> list[str]:
    pages = []
    params = {
        "action": "query",
        "format": "json",
        "list": "categorymembers",
        "cmtitle": f"Category:{category_name}",
        "cmnamespace": 0,
        "cmlimit": "max",
        "maxlag": 5,
    }
    while True:
        data = api_call_with_retry(session, params)
        for member in data.get("query", {}).get("categorymembers", []):
            pages.append(member["title"])
        if "continue" in data:
            params.update(data["continue"])
            time.sleep(BASE_DELAY)
        else:
            break
    return pages


def enumerate_category_pages_recursive(
    session: requests.Session, root_category: str, max_depth: int
) -> list[str]:
    pages = []
    seen_cats = set()
    seen_pages = set()
    frontier = [root_category]
    depth = 0
    while frontier:
        next_frontier = []
        for cat in frontier:
            if cat in seen_cats:
                continue
            seen_cats.add(cat)
            params = {
                "action": "query",
                "format": "json",
                "list": "categorymembers",
                "cmtitle": f"Category:{cat}",
                "cmlimit": "max",
                "maxlag": 5,
            }
            while True:
                data = api_call_with_retry(session, params)
                for member in data.get("query", {}).get("categorymembers", []):
                    if member["ns"] == 0:
                        if member["title"] not in seen_pages:
                            seen_pages.add(member["title"])
                            pages.append(member["title"])
                    elif member["ns"] == 14 and depth < max_depth:
                        sub = member["title"].removeprefix("Category:")
                        if sub not in seen_cats:
                            next_frontier.append(sub)
                if "continue" in data:
                    params.update(data["continue"])
                    time.sleep(BASE_DELAY)
                else:
                    break
            time.sleep(BASE_DELAY)
        frontier = next_frontier
        depth += 1
    return pages


def fetch_timestamps(session: requests.Session, titles: list[str]) -> dict[str, str]:
    touched = {}
    for i in range(0, len(titles), 50):
        batch = titles[i : i + 50]
        params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(batch),
            "prop": "info",
            "maxlag": 5,
        }
        data = api_call_with_retry(session, params)
        for page_info in data.get("query", {}).get("pages", {}).values():
            if "missing" in page_info or page_info.get("pageid", 0) <= 0:
                touched[page_info.get("title", "")] = None
            elif "touched" in page_info:
                touched[page_info["title"]] = page_info["touched"]
        if i + 50 < len(titles):
            time.sleep(BASE_DELAY)
    return touched


def fetch_page_content_batch(
    session: requests.Session, titles: list[str]
) -> dict[str, tuple[str, bool]]:
    results = {}
    for batch_start in range(0, len(titles), BATCH_SIZE):
        batch = titles[batch_start : batch_start + BATCH_SIZE]
        params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(batch),
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "maxlag": 5,
            "redirects": "",
        }
        data = api_call_with_retry(session, params)
        pages = data.get("query", {}).get("pages", {})
        for page_info in pages.values():
            pageid = page_info.get("pageid", 0)
            title = page_info.get("title", "")
            if pageid < 0:
                results[title] = ("", True)
                continue
            if "missing" in page_info:
                results[title] = ("", True)
                continue
            revisions = page_info.get("revisions", [])
            if revisions and "slots" in revisions[0]:
                content = revisions[0]["slots"]["main"].get("*", "")
                results[title] = (content, False)
            else:
                results[title] = ("", True)
        if batch_start + BATCH_SIZE < len(titles):
            time.sleep(BASE_DELAY)
    return results


def write_page_content(title: str, content: str, filename: str) -> bool:
    file_path = WIKI_DIR / filename
    try:
        temp_path = file_path.with_suffix(".tmp")
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, file_path)
        return True
    except Exception as e:
        print(f"[{_ts()}]   Failed to write {filename}: {e}")
        return False


def remove_stale_files(meta: dict, current_titles: set[str]) -> None:
    pages_meta = meta.get("pages", {})
    for title, info in pages_meta.items():
        if title not in current_titles:
            filename = info.get("filename")
            if filename:
                file_path = WIKI_DIR / filename
                if file_path.exists():
                    try:
                        file_path.unlink()
                        print(f"[{_ts()}]   Removed stale: {filename}")
                    except Exception as e:
                        print(f"[{_ts()}]   Failed to remove {filename}: {e}")


def remove_orphan_files(meta: dict) -> int:
    """Delete .txt files on disk not tracked by meta.

    Orphans are leftovers from older, broader enumerations or aborted runs;
    they are not part of the current sync scope and would otherwise load into
    the index forever. Returns the number of files removed.
    """
    tracked = {
        info.get("filename") for info in meta.get("pages", {}).values() if info.get("filename")
    }
    removed = 0
    for f in WIKI_DIR.glob("*.txt"):
        if f.name not in tracked:
            try:
                f.unlink()
                removed += 1
            except Exception as e:
                print(f"[{_ts()}]   Failed to remove orphan {f.name}: {e}")
    if removed:
        print(f"[{_ts()}]   Removed {removed} orphaned .txt file(s) not in meta")
    return removed


def _ts() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def main() -> int:
    WIKI_DIR.mkdir(parents=True, exist_ok=True)

    for f in WIKI_DIR.glob("*.tmp"):
        with contextlib.suppress(Exception):
            f.unlink()

    print(f"[{_ts()}] Connecting to {WIKI_HOST}...")
    session = make_session()

    all_titles = []
    if TARGET_CATEGORIES:
        print(f"[{_ts()}] Targeting categories: {', '.join(TARGET_CATEGORIES)}")
        for cat_name in TARGET_CATEGORIES:
            try:
                pages = enumerate_category_pages(session, cat_name)
                all_titles.extend(pages)
                print(f"[{_ts()}]   Category '{cat_name}': {len(pages)} pages")
                time.sleep(BASE_DELAY)
            except Exception as e:
                print(f"[{_ts()}]   Category '{cat_name}' failed: {e}")
                return 1
        for root, max_depth in RECURSIVE_CATEGORIES.items():
            try:
                pages = enumerate_category_pages_recursive(session, root, max_depth)
                all_titles.extend(pages)
                print(
                    f"[{_ts()}]   Recursive category '{root}' (depth {max_depth}): {len(pages)} pages"
                )
                time.sleep(BASE_DELAY)
            except Exception as e:
                print(f"[{_ts()}]   Recursive category '{root}' failed: {e}")
                return 1
    else:
        print(f"[{_ts()}] No category filter set. Fetching all wiki pages...")
        params = {
            "action": "query",
            "format": "json",
            "list": "allpages",
            "apnamespace": 0,
            "aplimit": "max",
            "maxlag": 5,
        }
        while True:
            try:
                data = api_call_with_retry(session, params)
                for page in data.get("query", {}).get("allpages", []):
                    all_titles.append(page["title"])
                if "continue" in data:
                    params.update(data["continue"])
                    time.sleep(BASE_DELAY)
                else:
                    break
            except Exception as e:
                print(f"[{_ts()}]   Allpages failed: {e}")
                return 1

    if WIKI_TITLE_EXTRAS:
        all_titles.extend(WIKI_TITLE_EXTRAS)
        print(
            f"[{_ts()}] Explicit titles ({len(WIKI_TITLE_EXTRAS)}): {', '.join(WIKI_TITLE_EXTRAS)}"
        )

    unique_titles = list(dict.fromkeys(all_titles))
    total_count = len(unique_titles)
    print(f"[{_ts()}] Queue verified. Found {total_count} unique items to evaluate.")

    meta = load_metadata()
    pages_meta = meta.setdefault("pages", {})

    title_to_filename = {}
    for title in unique_titles:
        if title in pages_meta and "filename" in pages_meta[title]:
            title_to_filename[title] = pages_meta[title]["filename"]
        else:
            title_to_filename[title] = get_stable_filename(title)

    print(f"[{_ts()}] Fetching page timestamps for freshness check...")
    touched_map = fetch_timestamps(session, unique_titles)

    if len(touched_map) != len(unique_titles):
        missing_titles = set(unique_titles) - set(touched_map.keys())
        print(f"[{_ts()}]   WARNING: {len(missing_titles)} titles missing from timestamp response")
        for t in list(missing_titles)[:5]:
            print(f"[{_ts()}]     missing: {t}")
        return 1

    new_count = 0
    updated_count = 0
    skipped_count = 0

    to_download = []
    for title in unique_titles:
        filename = title_to_filename[title]
        file_path = WIKI_DIR / filename

        stored_touched = pages_meta.get(title, {}).get("touched")
        current_touched = touched_map.get(title)
        file_exists = file_path.exists() and file_path.stat().st_size > 0

        if file_exists and stored_touched == current_touched:
            skipped_count += 1
        elif file_exists and title not in pages_meta:
            pages_meta[title] = {"touched": current_touched, "filename": filename}
            skipped_count += 1
        else:
            to_download.append(title)

    if skipped_count:
        print(f"[{_ts()}] Skipped {skipped_count} unchanged pages.")

    if not to_download:
        print(f"[{_ts()}] All pages up-to-date.")
    else:
        print(f"[{_ts()}] Downloading {len(to_download)} pages...")

    for batch_start in range(0, len(to_download), BATCH_SIZE):
        batch = to_download[batch_start : batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(to_download) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"[{_ts()}]   Batch {batch_num}/{total_batches} ({len(batch)} pages)...")

        try:
            results = fetch_page_content_batch(session, batch)
        except Exception as e:
            print(f"[{_ts()}]   Batch fetch failed: {e}")
            print(f"[{_ts()}] Aborting sync.")
            return 1

        for title in batch:
            filename = title_to_filename[title]
            file_path = WIKI_DIR / filename
            current_touched = touched_map.get(title)
            file_exists = file_path.exists() and file_path.stat().st_size > 0

            content, is_missing = results.get(title, ("", True))

            if is_missing:
                if file_exists:
                    try:
                        file_path.unlink()
                        print(f"[{_ts()}]   Removed missing page file: {filename}")
                    except Exception as e:
                        print(f"[{_ts()}]   Failed to remove missing page file: {e}")
                pages_meta[title] = {"touched": current_touched, "filename": filename}
                continue

            if not file_exists:
                new_count += 1
            else:
                updated_count += 1

            if write_page_content(title, content, filename):
                pages_meta[title] = {"touched": current_touched, "filename": filename}
            else:
                print(f"[{_ts()}]   Failed to save {title}, aborting")
                return 1

        time.sleep(BASE_DELAY)

    current_title_set = set(unique_titles)
    remove_stale_files(meta, current_title_set)
    remove_orphan_files(meta)

    meta["pages"] = {k: v for k, v in pages_meta.items() if k in current_title_set}
    save_metadata(meta)

    print(
        f"[{_ts()}] Complete. {new_count} new, {updated_count} updated, {skipped_count} skipped. Saved to '{WIKI_DIR}'."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
