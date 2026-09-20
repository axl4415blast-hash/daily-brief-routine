"""editions/ の下を調べ、存在する号の一覧を editions/index.json に書き出すスクリプト。

使い方:
  python3 scripts/build_index.py
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
EDITIONS_DIR = REPO_ROOT / "editions"
HYPOTHESES_DIR = REPO_ROOT / "hypotheses"
INDEX_PATH = EDITIONS_DIR / "index.json"

SLOTS = ["morning", "noon", "evening"]
# 大きいほど先頭(sort側でreverse=Trueを使うため): evening→noon→morning の順にしたい
SLOT_ORDER = {"evening": 2, "noon": 1, "morning": 0}
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def count_hypotheses(date, slot):
    path = HYPOTHESES_DIR / f"{date}-{slot}.json"
    if not path.exists():
        return 0
    try:
        data = load_json(path)
    except (json.JSONDecodeError, OSError) as e:
        print(f"skip (仮説ファイルを読めません): {path.relative_to(REPO_ROOT)} ({e})")
        return 0
    hypotheses = data.get("hypotheses")
    if not isinstance(hypotheses, list):
        return 0
    return len(hypotheses)


def build_entry(date, slot, edition_path):
    try:
        data = load_json(edition_path)
    except (json.JSONDecodeError, OSError) as e:
        print(f"skip (紙面JSONを読めません): {edition_path.relative_to(REPO_ROOT)} ({e})")
        return None

    verification = data.get("verification") or {}

    return {
        "date": date,
        "slot": slot,
        "edition_id": data.get("edition_id", f"{date}-{slot}"),
        "generated_at": data.get("generated_at"),
        "market_open": data.get("market_open"),
        "hypotheses_count": count_hypotheses(date, slot),
        "edition_path": f"editions/{date}/{slot}.json",
        "hypotheses_path": f"hypotheses/{date}-{slot}.json",
    }


def main():
    if not EDITIONS_DIR.exists():
        print(f"editionsディレクトリがありません: {EDITIONS_DIR}")
        return 1

    entries = []
    for date_dir in sorted(EDITIONS_DIR.iterdir()):
        if not date_dir.is_dir():
            continue
        if not DATE_DIR_RE.match(date_dir.name):
            continue
        date = date_dir.name
        for slot in SLOTS:
            edition_path = date_dir / f"{slot}.json"
            if not edition_path.exists():
                continue
            entry = build_entry(date, slot, edition_path)
            if entry is not None:
                entries.append(entry)

    entries.sort(key=lambda e: (e["date"], SLOT_ORDER[e["slot"]]), reverse=True)

    output = {
        "generated_at": dt.datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
        "editions": entries,
    }

    with INDEX_PATH.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"書き出しました: {INDEX_PATH.relative_to(REPO_ROOT)} ({len(entries)}件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
