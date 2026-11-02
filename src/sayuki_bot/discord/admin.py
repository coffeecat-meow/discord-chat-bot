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

    @group.command(name="debug_last", description="檢視最近回合各階段時間")
    async def debug_last(interaction: discord.Interaction):
        if not await admin(interaction):
            return
        ids = turn_ids(interaction.guild.id, 1)
        rows = (
            bot.db.connection.execute(
                "SELECT stage,data,created_at FROM traces WHERE turn_id=? ORDER BY id", (ids[0],)
            ).fetchall()
            if ids
            else []
        )
        await show(interaction, [dict(r) for r in rows])

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
