"""Conservative lexical selection for automatic event context, not memory search.

No source fetch, model call or causal/semantic expansion. Unmatched events stay
searchable. Profiles keep their existing candidate slots and order.
"""

import re
import unicodedata
from datetime import datetime

from sayuki_bot.memory.service import terms

# Remove common temporal/conversational scaffolding before making CJK bigrams:
# subtracting only '今天' afterwards would still leave spurious boundary bigrams.
_SCAFFOLD = re.compile(
    r"前天|昨天|今天|明天|後天|昨晚|今晚|現在|目前|最近|剛剛|剛才|之前|以後|後來|"
    r"早安|午安|晚安|你好|謝謝|怎麼樣|怎麼|什麼|這個|那個|這些|那些|這邊|那邊|"
    r"我們|你們|他們|她們|我的|你的|他的|她的|自己|還在|已經|正在|可以|一下|好嗎|還好"
)
_ENGLISH_SCAFFOLD = frozenset(
    "a an the i me my you your we our he him his she her they their it its "
    "is am are was were be been do does did have has had this that these those "
    "today yesterday tomorrow recently now later before after morning evening "
    "hello hi thanks how what when where why of to in on at and or but for with".split()
)


def topic_terms(text):
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"<[@#][!&]?\d+>|https?://\S+", " ", text)
    text = _SCAFFOLD.sub(" ", text)
    return {
        t
        for t in terms(text)
        if 2 <= len(t) <= 64 and not t.isdigit() and t not in _ENGLISH_SCAFFOLD
    }


def event_queries(turn, reply_record):
    """Use only a reply that is actually included in the compiled request."""
    current = turn.event
    mentions = set(re.findall(r"<@!?(\d+)>", current.text))
    mentions.discard(str(turn.bot_id))
    targets = mentions or {str(current.author_id)}
    queries = [("current", topic_terms(current.text))]
    if (
        reply_record
        and reply_record.get("message_id") == f"msg_{current.reply_to}"
        and reply_record.get("text")
        and datetime.fromisoformat(reply_record["time"]) <= current.created_at
    ):
        queries.append(("reply", topic_terms(reply_record["text"])))
        if not mentions and reply_record["author_id"] != str(turn.bot_id):
            targets = {reply_record["author_id"]}
    return queries, targets


def select_related(candidates, original_subjects, queries, event_subjects, limit=10):
    """Rank events inside the old event slots, leaving non-event slots intact."""
    original_subjects = {str(s) for s in original_subjects}
    slots = [r for r in candidates if r["subject_id"] in original_subjects][:limit]
    # Extra targets (explicit mentions / reply authors) may fill spare slots,
    # but never introduce their profiles or displace the old non-event slots.
    capacity = limit - sum(r["kind"] != "event" for r in slots)
    matched, decisions = [], []
    for index, row in enumerate(candidates):
        if row["kind"] != "event":
            continue
        matches = {}
        if row["subject_id"] in event_subjects:
            available = topic_terms(row.get("_index_text", row["key"]) + " " + row["value"])
            matches = {
                origin: sorted(available & query) for origin, query in queries if available & query
            }
        score = len({word for words in matches.values() for word in words})
        decisions.append(
            {
                "id": row["id"],
                "reason": "matched"
                if score
                else "other_subject"
                if row["subject_id"] not in event_subjects
                else "no_topic_overlap",
                "matches": {k: v[:8] for k, v in matches.items()},
            }
        )
        if score:
            matched.append((score, index, row))
    ranked = iter(
        [r for _, _, r in sorted(matched, key=lambda item: (-item[0], item[1]))][:capacity]
    )
    selected = []
    for row in slots:
        if row["kind"] != "event":
            selected.append(row)
        elif (replacement := next(ranked, None)) is not None:
            selected.append(replacement)
    selected.extend(ranked)
    return selected, decisions
