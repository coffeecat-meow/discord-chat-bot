"""Keep bounded tool evidence valid; never turn an uncertain action into success."""

import json

# Below this a string is not worth cutting further; fall back to omission.
_MIN_KEEP = 200


def _longest_string(value, path=()):
    """Path and length of the longest string inside nested dicts/lists."""
    best = (None, -1)
    if isinstance(value, str):
        return path, len(value)
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, child in items:
        found = _longest_string(child, (*path, key))
        if found[1] > best[1]:
            best = found
    return best


def _replace(value, path, new):
    if not path:
        return new
    copy = dict(value) if isinstance(value, dict) else list(value)
    copy[path[0]] = _replace(value[path[0]], path[1:], new)
    return copy


def _truncate_to_fit(report, fits):
    """Cut the longest text fields until fits(report) holds, keeping status intact."""
    if not isinstance(report, dict) or "result" not in report:
        return None
    trial = dict(report)
    for _ in range(50):
        if fits(trial):
            return trial
        path, length = _longest_string(trial["result"])
        if path is None or length <= _MIN_KEEP:
            return None
        current = trial["result"]
        for key in path:
            current = current[key]
        original = trial.get("_original_chars", {}).get(path, length)
        # Shrink by roughly the overflow; JSON escaping makes this approximate,
        # so the loop re-measures.
        keep = max(_MIN_KEEP, int(len(current) * 0.8) - 100)
        cut = current[:keep] + f"…（字數上限截斷，原文共{original}字）"
        trial["result"] = _replace(trial["result"], path, cut)
        trial.setdefault("_original_chars", {})[path] = original
        trial["truncated"] = True
    return None


def bounded_reports(reports, budget):
    """JSON list, or scalar 0 when even the omission summaries cannot fit."""

    def encode(value):
        return json.dumps(value, ensure_ascii=False)

    def clean(report):
        return {k: v for k, v in report.items() if k != "_original_chars"}

    full = encode(reports)
    if len(full) <= budget:
        return full
    summaries = [
        {
            "tool": report.get("tool"),
            "status": report["status"],
            "result_omitted": True,
            "reason": "字數上限省略內容；查詢可縮小範圍或減少limit，勿重做動作。",
        }
        for report in reports
    ]
    if len(encode(summaries)) > budget:
        return "0"
    for index, report in enumerate(reports):
        trial = list(summaries)
        trial[index] = report
        if len(encode(trial)) <= budget:
            summaries = trial
            continue
        # Too long as a whole: keep the beginning of its text instead of
        # dropping everything (a long SEARCH/READ_WEB page used to vanish).
        def fits(candidate, index=index):
            attempt = list(summaries)
            attempt[index] = clean(candidate)
            return len(encode(attempt)) <= budget

        shortened = _truncate_to_fit(report, fits)
        if shortened is not None:
            summaries[index] = clean(shortened)
    return encode(summaries)
