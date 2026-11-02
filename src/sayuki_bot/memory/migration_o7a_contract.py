"""Private O7-A helper: the field contract every imported memory must meet.

Used only by ``memory/migration.py``. It adds no storage path of its own; the
importer still writes through the same ``memories`` table.

The column list written by the importer comes from ``INSERT_FIELDS`` here, never
from the keys of a caller-supplied record, so a record cannot choose ``id``,
claim ``basis='self_report'`` or reach a column the contract does not name.
"""

from __future__ import annotations

from datetime import datetime

KINDS = frozenset({"profile", "event", "self", "culture"})

# Columns the importer writes, in order. `basis` is always 'unknown': an import
# never creates `memory_sources`, so it can never support a stronger basis.
INSERT_FIELDS = (
    "guild_id",
    "channel_id",
    "subject_id",
    "kind",
    "key",
    "value",
    "source_id",
    "source_author",
    "updated_at",
    "expires_at",
    "provenance",
    "context",
    "basis",
)
REQUIRED = ("guild_id", "channel_id", "subject_id", "kind", "key", "value", "updated_at", "provenance")
OPTIONAL = ("source_id", "source_author", "expires_at", "context", "basis", "active")
# `id` would let a caller pick a row identity; it is refused by name.
FORBIDDEN = ("id",)

MAX_KEY = 100
MAX_VALUE = 2000
MAX_CONTEXT = 100
MAX_PROVENANCE = 200


class Rejected(Exception):
    """A record that does not meet the contract; `reason` is the report key."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _identifier(value, reason):
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not text.isdigit():
        raise Rejected(reason)
    return text


def _optional_identifier(value, field):
    if value is None or value == "":
        return None
    return _identifier(value, f"invalid_id:{field}")


def _time(value, field, *, optional=False):
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value, str):
        raise Rejected(f"invalid_time:{field}")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Rejected(f"invalid_time:{field}") from None
    # The source's own string is kept; the importer does not rewrite times.
    return value


def normalize(item):
    """Return ``(clean_record, normalized_fields)`` or raise ``Rejected``.

    ``normalized_fields`` names what was changed rather than rejected, so the
    report can say so: today only a claimed non-``unknown`` basis.
    """
    if not isinstance(item, dict):
        raise Rejected("invalid_record")
    for field in FORBIDDEN:
        if field in item:
            raise Rejected(f"forbidden_field:{field}")
    for field in item:
        if field not in REQUIRED and field not in OPTIONAL:
            raise Rejected(f"unknown_field:{field}")
    for field in REQUIRED:
        if field not in item or item[field] is None:
            raise Rejected(f"missing_field:{field}")

    active = item.get("active", 1)
    if active in (0, False, "0"):
        # A withdrawn or invalidated history row; importing it as a new active
        # row would revive exactly what the source had retired.
        raise Rejected("inactive_history")

    clean = {
        "guild_id": _identifier(item["guild_id"], "invalid_scope_id"),
        "channel_id": _identifier(item["channel_id"], "invalid_scope_id"),
        "subject_id": _identifier(item["subject_id"], "invalid_subject_id"),
    }
    if not isinstance(item["kind"], str) or item["kind"] not in KINDS:
        raise Rejected("invalid_kind")
    clean["kind"] = item["kind"]
    for field in ("key", "value", "provenance"):
        if not isinstance(item[field], str):
            raise Rejected(f"invalid_type:{field}")
    if not item["key"].strip():
        raise Rejected("empty_key")
    if not item["value"].strip():
        raise Rejected("empty_value")
    if len(item["provenance"]) > MAX_PROVENANCE:
        raise Rejected("invalid_type:provenance")
    clean["key"], clean["value"], clean["provenance"] = item["key"], item["value"], item["provenance"]
    clean["updated_at"] = _time(item["updated_at"], "updated_at")
    clean["expires_at"] = _time(item.get("expires_at"), "expires_at", optional=True)
    clean["source_id"] = _optional_identifier(item.get("source_id"), "source_id")
    clean["source_author"] = _optional_identifier(item.get("source_author"), "source_author")
    context = item.get("context", "") or ""
    if not isinstance(context, str) or len(context) > MAX_CONTEXT:
        raise Rejected("invalid_type:context")
    clean["context"] = context

    normalized = []
    if item.get("basis", "unknown") != "unknown":
        normalized.append("basis")
    clean["basis"] = "unknown"
    return clean, normalized


def oversized(clean):
    return len(clean["value"]) > MAX_VALUE or len(clean["key"]) > MAX_KEY


def identity(clean):
    """Match key for existing rows and in-batch duplicates.

    ``self`` memory is global per (subject, key) in ``MemoryService`` — it is read
    and updated without guild or channel — so importing it under a second scope
    must match the first rather than add a duplicate key.
    """
    if clean["kind"] == "self":
        return ("self", clean["subject_id"], clean["key"])
    return (
        clean["guild_id"],
        clean["channel_id"],
        clean["subject_id"],
        clean["kind"],
        clean["key"],
        clean["context"],
    )


def existing_rows(connection, clean):
    if clean["kind"] == "self":
        return connection.execute(
            "SELECT id,value,active FROM memories WHERE kind='self' AND subject_id=? AND key=?",
            (clean["subject_id"], clean["key"]),
        ).fetchall()
    return connection.execute(
        "SELECT id,value,active FROM memories WHERE guild_id=? AND channel_id=? AND subject_id=?"
        " AND kind=? AND key=? AND context=?",
        identity(clean),
    ).fetchall()


def event_time(value):
    """Parse a chat_events timestamp or raise ``Rejected``."""
    if not isinstance(value, str) or not value:
        raise Rejected("invalid_time:timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Rejected("invalid_time:timestamp") from None
