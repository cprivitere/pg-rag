import json
from pathlib import Path

from pgrag.config import CDN_DIR


def load_database(db, cdn_dir=None):
    cdn_dir = Path(cdn_dir) if cdn_dir else CDN_DIR
    for file in sorted(cdn_dir.glob("*.json")):
        table_name = file.stem.lower()

        print(f"Loading {table_name}...")

        with open(file, encoding="utf-8") as f:
            db.add_table(table_name, json.load(f))

    print(f"Loaded {len(db.tables)} tables.")
