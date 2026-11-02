"""Source attribution and dependency checks shared by every profile writer."""

BASIS_NOTES = {
    "self_report": "本人陳述或確認；仍須保留原有時間與情境，不擴大成習慣。",
    "reported": "他人轉述；不是本人確認的事實，不省略說法的來源與不確定性。",
    "inferred": "紗月的推測／印象；可能有誤，不說成本人曾說過，可由本人更正。",
    "unknown": "舊資料尚未分類；不能當成已確認事實。",
}


def validate_sources(db, event, evidence, channels):
    sources = {str(e.id): e for e in [event, *(evidence or ())]}
    if len(sources) > 8:
        raise ValueError("Profile evidence exceeds 8 messages")
    channels = {str(c) for c in channels}
    for item in sources.values():
        if item.guild_id != event.guild_id or str(item.channel_id) not in channels:
            raise PermissionError("Profile evidence is outside readable scope")
        if not db.memory_enabled(item.guild_id, item.author_id):
            raise PermissionError("Memory is disabled for evidence author")
        row = db.connection.execute(
            "SELECT * FROM events WHERE id=? AND deleted=0", (str(item.id),)
        ).fetchone()
        if not row or (
            row["guild_id"],
            row["channel_id"],
            row["author_id"],
            row["text"],
            row["created_at"],
        ) != (
            str(item.guild_id),
            str(item.channel_id),
            str(item.author_id),
            item.text,
            item.created_at.isoformat(),
        ):
            raise ValueError("Profile evidence is stale")
    return tuple(sources.values())


def source_rows(db, memory_id):
    return db.connection.execute(
        """SELECT s.event_id,e.guild_id,e.channel_id,e.author_id,e.created_at,e.deleted
        FROM memory_sources s LEFT JOIN events e ON e.id=s.event_id WHERE s.memory_id=?
        ORDER BY e.created_at,s.event_id""",
        (memory_id,),
    ).fetchall()


def sources_visible(db, rows, guild_id, channels):
    channels = {str(c) for c in channels}
    return all(
        r["guild_id"] == str(guild_id)
        and r["channel_id"] in channels
        and r["deleted"] == 0
        and db.memory_enabled(guild_id, r["author_id"])
        for r in rows
    )
