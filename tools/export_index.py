"""把完整的 SQLite 索引导出为可提交、可逐行审阅的 JSON 种子。"""

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def export(database: Path, output: Path) -> Path:
    uri = f"{Path(database).resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        meta = dict(db.execute("SELECT key, value FROM actress_index_meta"))
        if meta.get("full_build_id") or not meta.get("full_built_at"):
            raise ValueError("索引不是已完成的完整构建")
        entries = [
            [row[0], row[1], row[2]]
            for row in db.execute(
                "SELECT ref, name, furi FROM actress_index ORDER BY ref"
            )
        ]
    output = Path(output)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write("{\n")
        handle.write(f'  "index_version": {int(meta["index_version"])},\n')
        handle.write(f'  "full_built_at": {float(meta["full_built_at"])},\n')
        handle.write(f'  "rows": {len(entries)},\n')
        handle.write('  "entries": [\n')
        for index, entry in enumerate(entries):
            comma = "," if index + 1 < len(entries) else ""
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            handle.write(f"    {encoded}{comma}\n")
        handle.write("  ]\n}\n")
    temporary.replace(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "actress_index.json"
    )
    args = parser.parse_args()
    print(export(args.database, args.output))
