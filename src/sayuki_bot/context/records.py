"""Bounded, quoted conversation records shared by initial and tool contexts."""

import json


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False))


def event_record(event, bot_id, zone):
    return {
        "message_id": f"msg_{event.id}",
        "author": event.author_name,
        "author_id": str(event.author_id),
        "self": event.author_id == bot_id,
        "time": event.created_at.astimezone(zone).isoformat(),
        "reply_to": f"msg_{event.reply_to}" if event.reply_to else None,
        "text": event.text,
        "attachments": event.attachments,
    }


def bounded_record(record, budget):
    """Bound the entire JSON record, including identities and truncation flags.

    Keep complete ordinary records. Oversized records retain identity and a text
    prefix; attachment metadata/image observations are omitted with disclosure.
    If even identity cannot fit, the caller must disclose omission instead.
    """
    if encoded_size(record) <= budget:
        return record
    result = {**record, "truncated": True, "attachments": ()}
    result.pop("previous_image_description", None)
    result["author"] = result["author"][:80]
    text = result["text"]
    result["text"] = ""
    if encoded_size(result) > budget:
        return None
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        result["text"] = text[:middle]
        if encoded_size(result) <= budget:
            low = middle
        else:
            high = middle - 1
    result["text"] = text[:low]
    return result
