"""Read-only measurements. Missing usage is unknown, never zero tokens."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path


def summarize(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        turns = {}
        for row in db.execute("SELECT * FROM traces ORDER BY id"):
            item = turns.setdefault(
                row["turn_id"], {"turn_id": row["turn_id"], "stages": {}, "pacing_ms": 0}
            )
            data = json.loads(row["data"])
            item["stages"].setdefault(row["stage"], row["created_at"])
            if row["stage"] == "pacing":
                item["pacing_ms"] += data.get("duration_ms", 0)
        results = []
        for item in turns.values():
            stages = item.pop("stages")
            for name, start, end in [
                ("queue_ms", "queued", "started"),
                ("first_visible_ms", "queued", "first_visible"),
                ("total_ms", "queued", "finished"),
            ]:
                item[name] = (
                    (
                        datetime.fromisoformat(stages[end]) - datetime.fromisoformat(stages[start])
                    ).total_seconds()
                    * 1000
                    if start in stages and end in stages
                    else None
                )
            results.append(item)
        groups = {}
        for row in db.execute("SELECT * FROM usage ORDER BY id"):
            key = (row["role"], row["model"])
            group = groups.setdefault(
                key,
                dict(
                    role=key[0],
                    model=key[1],
                    calls=0,
                    errors=0,
                    duration_ms=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    missing_prompt_usage=0,
                    missing_completion_usage=0,
                ),
            )
            data = json.loads(row["data"])
            group["calls"] += 1
            group["errors"] += bool(data.get("error_type"))
            group["duration_ms"] += row["duration_ms"]
            for field, missing in [
                ("prompt_tokens", "missing_prompt_usage"),
                ("completion_tokens", "missing_completion_usage"),
            ]:
                value = data.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    group[field] += value
                else:
                    group[missing] += 1
        return {
            "turns": results,
            "usage_by_role_model": list(groups.values()),
            "notes": [
                "Token sums cover reported usage only; missing usage is counted separately.",
                "First visible includes queue and deliberate pacing; it is not streaming TTFT.",
                "Usage includes background and vision calls. No inferred monetary cost.",
                "No message text or persona content is exported.",
            ],
        }
    finally:
        db.close()
