import json

from pgrag.config import CDN_DIR


def load_database(db):
    for file in sorted(CDN_DIR.glob("*.json")):
        table_name = file.stem.lower()

        print(f"Loading {table_name}...")

        with open(file, encoding="utf-8") as f:
            db.add_table(table_name, json.load(f))

    print(f"Loaded {len(db.tables)} tables.")
