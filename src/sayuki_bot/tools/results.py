"""Keep bounded tool evidence valid; never turn an uncertain action into success."""

import json


def bounded_reports(reports, budget):
    """JSON list, or scalar 0 when even the omission summaries cannot fit."""

    def encode(value):
        return json.dumps(value, ensure_ascii=False)

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
    return encode(summaries)
