import discord
import json
from datetime import datetime, timezone
from sayuki_bot import __version__


def register_admin(bot):
    group = discord.app_commands.Group(name="sayuki", description="Sayuki管理")

    async def admin(interaction):
        if (
            interaction.user.id not in bot.config.admins
            or not interaction.guild
            or interaction.guild.id not in bot.runtime_guilds
        ):
            await interaction.response.send_message("沒有管理權限或伺服器尚未啟用", ephemeral=True)
            return False
        return True

    @group.command(name="status", description="查看運行狀態")
    async def status(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        await interaction.response.send_message(
            f"{__version__}｜進行中:{len(bot.engine.active)}｜佇列:{sum(q.qsize() for q in bot.engine.queues.values())}｜背景記憶:{bot.config.get('memory', 'background_enabled', False)}",
            ephemeral=True,
        )

    @group.command(name="interrupt", description="停止此頻道待發送內容")
    async def interrupt(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        count = bot.engine.interrupt(interaction.guild.id, interaction.channel_id)
        await interaction.response.send_message(f"已中止{count}個回合", ephemeral=True)

    @group.command(name="look", description="查看目前頻道，睡眠中需直接提及")
    async def look(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        if bot.config.sleep.sleeping(datetime.now(timezone.utc)):
            await interaction.response.send_message("目前睡眠中，請直接@她", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await bot.receive_interaction(
            interaction, "看看現在大家在聊什麼，不一定需要說話", reason="proactive"
        )
        await interaction.followup.send(
            "已排入查看" if result["status"] == "queued" else "這次未排入查看，請稍後再試",
            ephemeral=True,
        )

    @group.command(name="reload_prompt", description="重新讀取角色卡")
    async def reload_prompt(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        bot.compiler.persona = bot.config.path("persona").read_text(encoding="utf-8-sig")
        await interaction.response.send_message("已重讀角色卡", ephemeral=True)

    @group.command(name="reminders", description="查看目前伺服器待送提醒")
    async def reminders(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        rows = bot.db.connection.execute(
            "SELECT id,due_at,content FROM reminders WHERE guild_id=? AND status='pending' ORDER BY due_at LIMIT 10",
            (str(interaction.guild.id),),
        ).fetchall()
        await interaction.response.send_message(
            "\n".join(f"{r['id']} {r['due_at']} {r['content'][:60]}" for r in rows)
            or "沒有待送提醒",
            ephemeral=True,
        )

    @group.command(name="cancel_reminder", description="取消指定提醒")
    async def cancel(interaction: discord.Interaction, reminder_id: str):
        if not await admin(interaction):
            return
        with bot.db.connection:
            result = bot.db.connection.execute(
                "UPDATE reminders SET status='cancelled' WHERE id=? AND guild_id=? AND status='pending'",
                (reminder_id, str(interaction.guild.id)),
            )
        await interaction.response.send_message(
            "已取消" if result.rowcount else "未找到待送提醒", ephemeral=True
        )

    async def show(interaction, payload):
        text = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False, indent=2)
        )
        if len(text) <= 1800:
            await interaction.response.send_message(text or "沒有資料", ephemeral=True)
        else:
            import io

            await interaction.response.send_message(
                file=discord.File(io.BytesIO(text.encode()), filename="sayuki-diagnostic.json"),
                ephemeral=True,
            )

    @group.command(name="memory_user", description="檢視本頻道人物記憶與來源")
    async def memory_user(interaction: discord.Interaction, user_id: str):
        if not await admin(interaction):
            return
        if not user_id.isdigit():
            await show(interaction, "請使用數字ID")
            return
        await show(
            interaction,
            bot.memory.recall(
                interaction.guild.id, [interaction.channel_id], subjects=[user_id], limit=100
            ),
        )

    @group.command(name="memory_permanent", description="檢視自身永久記憶")
    async def memory_permanent(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        await show(interaction, bot.memory.self_memory())

    @group.command(name="memory_server", description="檢視本頻道的群組文化記憶")
    async def memory_server(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        rows = bot.db.connection.execute(
            "SELECT key,value,source_id,updated_at FROM memories WHERE guild_id=? AND channel_id=? AND kind='culture' AND active=1",
            (str(interaction.guild.id), str(interaction.channel_id)),
        ).fetchall()
        await show(interaction, [dict(r) for r in rows])

    @group.command(name="clear_status", description="清空動態狀態")
    async def clear_status(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        asleep = bot.config.sleep.sleeping(datetime.now(timezone.utc))
        await bot.change_presence(
            activity=discord.Game(bot.config.sleep.status) if asleep else None,
            status=discord.Status.idle if asleep else discord.Status.online,
        )
        bot.sayuki_status_until = None
        await show(interaction, "已清除動態狀態")

    @group.command(name="permissions", description="檢視目前頻道權限")
    async def permissions(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        perms = interaction.channel.permissions_for(interaction.guild.me)
        await show(
            interaction,
            {
                name: getattr(perms, name, False)
                for name in [
                    "view_channel",
                    "read_message_history",
                    "send_messages",
                    "add_reactions",
                    "attach_files",
                    "embed_links",
                    "send_polls",
                    "create_public_threads",
                    "manage_nicknames",
                    "moderate_members",
                ]
            },
        )

    def turn_ids(guild_id, limit=20):
        rows = bot.db.connection.execute(
            "SELECT turn_id FROM traces WHERE stage='queued' AND json_extract(data,'$.guild_id')=? ORDER BY id DESC LIMIT ?",
            (str(guild_id), limit),
        ).fetchall()
        return [r[0] for r in rows]

    REASONS = {
        "message": "一般訊息",
        "mention": "被提及",
        "name": "被叫名字",
        "reply": "回覆她的訊息",
        "proactive": "主動查看",
        "continuation": "接續對話",
        "cross_continuation": "跨頻道接續",
        "echo": "跟著複誦",
        "interaction": "按鈕／面板互動",
        "task": "任務",
        "reminder": "提醒",
    }
    OUTCOMES = {
        "completed": "完成",
        "cancelled": "已取消",
        "deferred": "延後（睡眠中）",
        "timed_out": "任務逾時",
        "failed_generation": "模型沒有可用回覆",
        "failed_verification": "原訊息驗證未通過",
        "error": "程式錯誤",
        "stopped": "中途停止",
    }
    TOOL_STATUS = {
        "ok": "成功",
        "rejected": "被拒絕（未執行）",
        "unavailable": "無法使用",
        "unknown": "不確定是否完成",
        "not_applied": "未套用",
        "already_attempted": "重複呼叫已略過",
        "limit": "超過單輪上限",
    }

    def taipei(value):
        from zoneinfo import ZoneInfo

        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")

    def seconds(ms):
        return f"{(ms or 0) / 1000:.1f}秒"

    def trace_summary(turn_id):
        rows = bot.db.connection.execute(
            "SELECT stage,data,created_at FROM traces WHERE turn_id=? ORDER BY id", (turn_id,)
        ).fetchall()
        stages = [(r["stage"], json.loads(r["data"] or "{}"), r["created_at"]) for r in rows]
        lines = []
        started = next((datetime.fromisoformat(t) for s, _, t in stages if s == "started"), None)
        first = next((datetime.fromisoformat(t) for s, _, t in stages if s == "first_visible"), None)
        finished = next((d for s, d, _ in stages if s == "finished"), None)
        timing = []
        if started and first:
            timing.append(f"第一段送出 {(first - started).total_seconds():.1f}秒")
        if finished:
            timing.append(f"共{finished.get('visible_chunks', 0)}段")
            timing.append(f"總計 {seconds(finished.get('duration_ms'))}")
        if timing:
            lines.append("耗時：" + "、".join(timing))
        for stage, data, _ in stages:
            if stage == "model_failover":
                lines.append(
                    f"模型切換備援：{data.get('role')} {data.get('error_type')}"
                    + (f"（{data['status_code']}）" if data.get("status_code") else "")
                )
            elif stage in {"model_fallback_failed", "model_fallback_unavailable"}:
                lines.append(f"備援模型也失敗：{data.get('role')} {data.get('error_type')}")
            elif stage == "unusable_completion":
                lines.append(f"模型回覆不可用：{data.get('reason') or data.get('finish_reason')}")
            elif stage == "cap_closeout":
                lines.append(f"工具輪數用完，改為收尾回覆（{data.get('reason')}）")
        return stages, lines

    def channel_label(guild, channel_id):
        channel = guild.get_channel_or_thread(int(channel_id)) if guild else None
        return f"#{channel.name} ({channel_id})" if channel else str(channel_id)

    def format_debug(guild, turn):
        record = turn.debug
        event = turn.event
        sent = record.get("sent", [])
        rounds = record.get("rounds", [])
        lines = [
            f"時間：{taipei(record.get('started_at', event.created_at))}",
            f"觸發原因：{REASONS.get(turn.reason, turn.reason)}",
            f"頻道：{channel_label(guild, event.channel_id)}",
            f"使用者：{event.author_name} ({event.author_id})",
            f"訊息ID：{event.id}",
            f"結果：{OUTCOMES.get(record.get('outcome'), record.get('outcome', '進行中'))}"
            + (f"（{record['error_type']}）" if record.get("error_type") else ""),
            f"是否回覆：{'是' if sent or record.get('aux_sent') else '否'}",
            f"是否NO_NEED：{'是' if any(r.get('silence') for r in rounds) else '否'}",
        ]
        if record.get("stop_reason"):
            lines.append(f"停止原因：{preview(record['stop_reason'], 300)}")
        if record.get("delivery_uncertain"):
            lines.append("送達狀態：不確定（可能部分送出）")
        participation = record.get("participation")
        if participation:
            lines.append(
                f"接話判斷：{'要接' if participation.get('should_reply') else '不接'}"
                f"（{participation.get('status', '')}）"
            )
        lines.append(f"原始訊息：{preview(event.text, 500)}")
        if not rounds:
            lines.append("模型回覆：未呼叫模型")
        for item in rounds:
            label = "初次模型回覆" if item["round"] == 1 else f"第{item['round']}輪模型回覆"
            lines.append(f"{label}：{preview(item['response'], 500) or '（空白）'}")
            if item.get("finish_reason") == "length":
                lines.append("  ⚠ 輸出長度用完被截斷")
            for error in item.get("errors", []):
                lines.append(f"  ⚠ 格式錯誤：{error}")
            for tool in item.get("tools", []):
                status = TOOL_STATUS.get(tool["status"], tool["status"])
                lines.append(f"- 工具 {tool['tool']}（{preview(tool['args'], 200)}）：{status}")
                if tool["detail"]:
                    lines.append(f"  結果：{preview(tool['detail'], 300)}")
        for aux in record.get("aux_sent", []):
            lines.append(f"另外送到 {channel_label(guild, aux['channel_id'])}：{preview(aux['text'], 300)}")
        lines.append(f"最後回覆：{preview(chr(10).join(sent), 800) if sent else '（沒有送出）'}")
        _, summary = trace_summary(record.get("turn_id", turn.id))
        lines.extend(summary)
        return "\n".join(lines)

    def preview(text, limit):
        text = (text or "").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    @group.command(name="debug_last", description="查看最近一次處理的debug摘要")
    async def debug_last(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        turn = bot.engine.last_debug.get(interaction.guild.id)
        if turn is not None:
            text = format_debug(interaction.guild, turn)
        else:
            # After a restart only timing traces survive; summarise them instead.
            ids = turn_ids(interaction.guild.id, 1)
            if not ids:
                await interaction.response.send_message("目前沒有debug紀錄。", ephemeral=True)
                return
            stages, summary = trace_summary(ids[0])
            queued = next((d for s, d, _ in stages if s == "queued"), {})
            lines = [
                "（重啟後還沒處理過新回合，只剩各階段時間，沒有對話內容）",
                f"時間：{taipei(stages[0][2])}" if stages else "",
                f"觸發原因：{REASONS.get(queued.get('reason'), queued.get('reason', '未記錄'))}",
                f"頻道：{channel_label(interaction.guild, queued['channel_id'])}"
                if queued.get("channel_id")
                else "",
                *summary,
            ]
            text = "\n".join(line for line in lines if line)
        if len(text) <= 1900:
            await interaction.response.send_message(f"```text\n{text}\n```", ephemeral=True)
        else:
            import io

            await interaction.response.send_message(
                "內容較長，完整內容在附件",
                file=discord.File(io.BytesIO(text.encode()), filename="sayuki-debug.txt"),
                ephemeral=True,
            )

    @group.command(name="logs", description="檢視近期模型用量與錯誤，不包含角色卡")
    async def logs(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        ids = turn_ids(interaction.guild.id)
        rows = (
            bot.db.connection.execute(
                f"SELECT role,model,duration_ms,data,created_at FROM usage WHERE turn_id IN ({','.join('?' for _ in ids)}) ORDER BY id DESC LIMIT 30",
                ids,
            ).fetchall()
            if ids
            else []
        )
        await show(interaction, [dict(r) for r in rows])

    @group.command(name="tool_stats", description="檢視近期動作送達狀態")
    async def tool_stats(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        ids = turn_ids(interaction.guild.id, 100)
        rows = (
            bot.db.connection.execute(
                f"SELECT kind,status,COUNT(*) AS count FROM receipts WHERE turn_id IN ({','.join('?' for _ in ids)}) GROUP BY kind,status",
                ids,
            ).fetchall()
            if ids
            else []
        )
        await show(interaction, [dict(r) for r in rows])

    @group.command(name="user_stats", description="檢視本伺服器觀測互動統計")
    async def user_stats(interaction: discord.Interaction, user_id: str):
        if not await admin(interaction):
            return
        row = bot.db.connection.execute(
            "SELECT COUNT(*) AS observed_messages,MIN(created_at) AS first_seen,MAX(created_at) AS last_seen FROM events WHERE guild_id=? AND author_id=? AND deleted=0",
            (str(interaction.guild.id), user_id),
        ).fetchone()
        await show(interaction, dict(row))

    @group.command(name="memory_enabled", description="設定成員是否允許保存記憶")
    async def memory_enabled(interaction: discord.Interaction, user_id: str, enabled: bool):
        if not await admin(interaction):
            return
        if not user_id.isdigit():
            await show(interaction, "請使用數字ID")
            return
        from sayuki_bot.memory.governance import Governance

        Governance(bot.db).set_enabled(interaction.guild.id, user_id, enabled)
        await show(interaction, {"user_id": user_id, "enabled": enabled})

    @group.command(name="data_export", description="匯出本伺服器指定使用者資料")
    async def data_export(interaction: discord.Interaction, user_id: str):
        if not await admin(interaction):
            return
        from sayuki_bot.memory.governance import Governance

        await show(interaction, Governance(bot.db).export(interaction.guild.id, user_id))

    @group.command(name="data_erase", description="清除本伺服器指定使用者資料並停止記憶")
    async def data_erase(interaction: discord.Interaction, user_id: str):
        if not await admin(interaction):
            return
        if not user_id.isdigit():
            await show(interaction, "請使用數字ID")
            return
        from sayuki_bot.memory.governance import Governance

        # Clear ephemeral context too, and stop work that could recreate erased data.
        bot.engine.cancel_user(interaction.guild.id, int(user_id))
        identifiers = [
            int(r[0])
            for r in bot.db.connection.execute(
                "SELECT id FROM events WHERE guild_id=? AND author_id=?",
                (str(interaction.guild.id), user_id),
            )
        ]
        for identifier in identifiers:
            bot.engine.invalidate_source(identifier)
        for channel_id, history in bot.history.items():
            retained = [
                e
                for e in history
                if not (e.guild_id == interaction.guild.id and str(e.author_id) == user_id)
            ]
            history.clear()
            history.extend(retained)
        await show(interaction, Governance(bot.db).erase(interaction.guild.id, user_id))

    @group.command(name="data_restore_import", description="本人明確允許管理員恢復先前清除的舊資料")
    async def data_restore_import(interaction: discord.Interaction, confirm: bool = False):
        # No target-user argument: an administrator cannot confirm for someone
        # else, and this control is never exposed as an LLM tool.
        if not interaction.guild or interaction.guild.id not in bot.runtime_guilds:
            await show(interaction, "伺服器尚未啟用")
            return
        if confirm is not True:
            await show(
                interaction,
                "確認要允許管理員恢復你先前清除的舊資料時，再使用 confirm=true；這不會自動開啟記憶或執行匯入。",
            )
            return
        from sayuki_bot.memory.governance import Governance

        await show(
            interaction,
            Governance(bot.db).allow_import_restore(
                interaction.guild.id, interaction.user.id, confirmed=True
            ),
        )

    @group.command(name="trust_list", description="檢視目前信任伺服器")
    async def trust_list(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        await show(interaction, {"trusted_guild_ids": sorted(bot.runtime_guilds)})

    @group.command(name="trust_add", description="啟用Bot已加入的指定伺服器")
    async def trust_add(interaction: discord.Interaction, guild_id: str):
        if not await admin(interaction):
            return
        if not guild_id.isdigit() or not bot.get_guild(int(guild_id)):
            await show(interaction, "Bot不在該伺服器")
            return
        with bot.db.connection:
            bot.db.connection.execute(
                "INSERT INTO trusted_guilds VALUES(?,1) ON CONFLICT(guild_id) DO UPDATE SET enabled=1",
                (guild_id,),
            )
        bot.runtime_guilds.add(int(guild_id))
        await interaction.response.defer(ephemeral=True)
        target = discord.Object(id=int(guild_id))
        bot.tree.copy_global_to(guild=target)
        await bot.tree.sync(guild=target)
        await interaction.followup.send("已啟用", ephemeral=True)

    @group.command(name="trust_remove", description="停止指定伺服器互動")
    async def trust_remove(interaction: discord.Interaction, guild_id: str):
        if not await admin(interaction):
            return
        if not guild_id.isdigit():
            await show(interaction, "請使用數字ID")
            return
        with bot.db.connection:
            bot.db.connection.execute(
                "INSERT INTO trusted_guilds VALUES(?,0) ON CONFLICT(guild_id) DO UPDATE SET enabled=0",
                (guild_id,),
            )
        bot.runtime_guilds.discard(int(guild_id))
        for key in list(bot.engine.queues):
            if key[0] == int(guild_id):
                bot.engine.interrupt(*key)
        await show(interaction, "已停止互動；已儲存資料未刪除")

    bot.tree.add_command(group)
