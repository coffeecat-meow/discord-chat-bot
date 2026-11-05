"""還原某個回合送給模型的完整內容（含系統提示詞）。

用法：
    python scripts/show_turn.py <turn_id>        # 回合 ID 可只給開頭幾碼
    python scripts/show_turn.py --last           # 最近一次聊天回合
    python scripts/show_turn.py <turn_id> --json # 輸出可直接重送給模型的 messages JSON
"""

import argparse
import json
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"


def read(name):
    rows = []
    for path in (LOGS / f"{name}.jsonl.1", LOGS / f"{name}.jsonl"):
        if path.exists():
            with path.open(encoding="utf-8") as file:
                rows.extend(json.loads(line) for line in file if line.strip())
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("turn_id", nargs="?")
    parser.add_argument("--last", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    calls = read("invocation")
    if args.last:
        chats = [c for c in calls if c["call_type"].split(":")[0] in {"chat", "task"}]
        if not chats:
            raise SystemExit("沒有聊天紀錄")
        args.turn_id = chats[-1]["turn_id"]
    if not args.turn_id:
        parser.error("請給回合 ID 或 --last")

    selected = [c for c in calls if str(c.get("turn_id", "")).startswith(args.turn_id)]
    if not selected:
        raise SystemExit(f"找不到回合 {args.turn_id}")
    prompts = {p["sha256"]: p["content"] for p in read("system_prompts")}

    def restore(message):
        if message.get("system_prompt_omitted"):
            content = prompts.get(message.get("sha256"))
            if content is None:
                content = f"（找不到原文：此紀錄早於提示詞保存功能，或雜湊 {message.get('sha256')} 不在 system_prompts.jsonl）"
            return {"role": message["role"], "content": content}
        return {"role": message["role"], "content": message["content"]}

    for call in selected:
        messages = [restore(m) for m in call["messages"]]
        if args.json:
            print(json.dumps({"call_type": call["call_type"], "model": call["model"], "messages": messages}, ensure_ascii=False, indent=2))
            continue
        print("=" * 70)
        print(f"{call['time']}  {call['call_type']}  {call['route']}  {call['model']}  "
              f"{'成功' if call['success'] else '失敗：' + call['error']}  {call['duration_ms']}ms")
        for index, message in enumerate(messages):
            print(f"\n--- [{index}] {message['role']} ({len(message['content'])}字) ---")
            print(message["content"])
        if call.get("reasoning"):
            print(f"\n--- 推理過程（{call.get('reasoning_tokens') or '?'} tokens）---\n{call['reasoning']}")
        elif call.get("reasoning_tokens"):
            print(f"\n--- 推理過程：用了 {call['reasoning_tokens']} tokens，但服務商沒有回傳內容 ---")
        print(f"\n--- 模型輸出 ---\n{call['output']}\n")


if __name__ == "__main__":
    main()
