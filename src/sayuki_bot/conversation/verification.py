"""Bounded literal read requests, not a general intent or fact classifier."""

from dataclasses import dataclass, field
import json
import re

URL = re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
ID = re.compile(r"#?msg_(\d{1,20})")
TARGET = r"(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/\d+/\d+/\d+|#?msg_\d{1,20})(?!\d)"
REQUEST = re.compile(r"(?:打開|讀取|查證)[^\n。！？!?；;，,\ufffc]{0,60}?" + TARGET)
# Short 讀 is deliberately narrower than 讀取: a clause-leading imperative,
# followed directly by a literal target, not 讀書/讀完 or reported past reading.
SHORT_REQUEST = re.compile(
    r"\s*(?:<@!?\d+>\s*)?"
    r"(?:(?:請你?|麻煩你?|幫我|幫忙|先|再|也|現在|接著|重新)\s*){0,2}"
    r"讀(?:一下)?\s*" + TARGET
)
CLAUSE = re.compile(r"[^\n。！？!?；;，,]+")
SENTENCE_END = re.compile(r"[\n。！？!?；;]")
NON_REQUEST = re.compile(r"例如|範例|如果|假如|你會")
NEGATION = re.compile(r"不用|不必|不要|別|無須|不需要")
QUOTED = re.compile(r'```.*?(?:```|$)|`[^`\n]*`|『[^』]*』|「[^」]*」|"[^"]*"', re.S)


def reference(value):
    value = value.strip()
    match = URL.fullmatch(value)
    if match:
        return "url:" + "/".join(str(int(x)) for x in match.groups())
    match = ID.fullmatch(value)
    return "id:" + str(int(match[1])) if match else None


@dataclass
class Verification:
    guild_id: int
    required: dict[str, str] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_text(cls, text, guild_id):
        # Quoted examples/history are not instructions to perform a read.
        # Mask instead of deleting, so quoted words cannot join into a command.
        text = QUOTED.sub(lambda m: re.sub(r"[^\n]", "\ufffc", m[0]), text[:4096])
        result = cls(guild_id)
        sentence_start = 0
        for clause in CLAUSE.finditer(text):
            # Conditions/examples extend across commas; an unrelated negation
            # only constrains its own clause. A sentence boundary resets both.
            breaks = list(SENTENCE_END.finditer(text, sentence_start, clause.start()))
            if breaks:
                sentence_start = breaks[-1].end()
            matches = list(REQUEST.finditer(clause[0]))
            if short := SHORT_REQUEST.match(clause[0]):
                matches.append(short)
            for match in sorted(matches, key=lambda m: m.start()):
                position = clause.start() + match.start()
                if NON_REQUEST.search(text[sentence_start:position]):
                    continue
                if NEGATION.search(clause[0][:match.start()]):
                    continue
                result.required[reference(match[1])] = match[1]
                if len(result.required) == 5:
                    return result
        return result

    @property
    def pending(self):
        return self.required.keys() - self.outcomes.keys()

    @property
    def successful(self):
        return not self.pending and all(x == "live_verified" for x in self.outcomes.values())

    def snapshot(self):
        return {"pending": sorted(self.pending), "outcomes": dict(self.outcomes)}

    def observe(self, call, report):
        if call.name != "READ_MESSAGE" or report.get("status") == "already_attempted":
            return
        key = reference(call.text)
        if key is None:
            return
        keys = {key}
        if key.startswith(f"url:{self.guild_id}/"):
            keys.add("id:" + key.rsplit("/", 1)[-1])
        availability = (report.get("result") or {}).get("availability") if report.get("status") == "ok" and isinstance(report.get("result"), dict) else None
        for target in self.pending & keys:
            self.outcomes[target] = availability or report.get("status", "unavailable")

    def instruction(self):
        targets = [self.required[k] for k in sorted(self.pending)]
        return (
            "本回合明確要求打開原訊息：" + json.dumps(targets, ensure_ascii=False)
            + "。須用READ_MESSAGE取得目前原文，不能以已可見歷史或TOOL_HELP代替這次查證。"
            "若工具實際拒絕或讀不到，依回傳說明未完成，不冒充讀過。"
        )

    def retry_messages(self):
        return [
            {"role": "assistant", "content": "（上一段未完成指定的原訊息查證，文字未送達。）"},
            {"role": "user", "content": self.instruction()},
        ]
