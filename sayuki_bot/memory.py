from __future__ import annotations

import asyncio
import json
import os
import re
from copy import deepcopy
from datetime import datetime
from typing import Any

import aiofiles


UNKNOWN = "未知"
MAX_MEMORY_CONTEXT_CHARS = int(os.getenv("MAX_MEMORY_CONTEXT_CHARS", "120000"))


def _now_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _default_user_memory(user_id: str, name: str = UNKNOWN) -> dict[str, Any]:
    return {
        "user_id": str(user_id),
        "name": name,
        "basic_info": {
            "nickname": UNKNOWN,
            "location": UNKNOWN,
            "mbti": UNKNOWN,
        },
        "hobbies": [],
        "relationship": UNKNOWN,
        "recent_events": UNKNOWN,
        "gossip_pool": [],
        "important_events": [],
    }


def _dedupe_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)


def _merge_value(old_value: Any, new_value: Any) -> Any:
    if isinstance(old_value, dict) and isinstance(new_value, dict):
        merged = deepcopy(old_value)
        for key, value in new_value.items():
            merged[key] = _merge_value(merged.get(key), value)
        return merged

    if isinstance(old_value, list):
        additions = new_value if isinstance(new_value, list) else [new_value]
        merged = deepcopy(old_value)
        seen = {_dedupe_key(item) for item in merged}
        for item in additions:
            key = _dedupe_key(item)
            if key not in seen:
                merged.append(item)
                seen.add(key)
        return merged

    return deepcopy(new_value)


def _truncate_context(text: str) -> str:
    if len(text) <= MAX_MEMORY_CONTEXT_CHARS:
        return text

    return text[:MAX_MEMORY_CONTEXT_CHARS] + "\n...（記憶內容過長，已截斷）"


class MemoryManager:
    def __init__(self, db_file: str = "memory.json", permanent_db_file: str = "permanent_memory.json"):
        self.db_file = db_file
        self.permanent_db_file = permanent_db_file
        self.data: dict[str, dict[str, Any]] = {}
        self.permanent_data = {"facts": []}
        self.lock = asyncio.Lock()

    def _normalize_loaded_memory(self, loaded: Any) -> dict[str, dict[str, Any]]:
        if not loaded:
            return {}

        if isinstance(loaded, dict) and "users" in loaded:
            loaded = loaded["users"]

        if isinstance(loaded, dict) and loaded.get("user_id"):
            profile = loaded
            return {str(profile["user_id"]): profile}

        if isinstance(loaded, list):
            normalized = {}
            for profile in loaded:
                if isinstance(profile, dict) and profile.get("user_id"):
                    normalized[str(profile["user_id"])] = profile
            return normalized

        if isinstance(loaded, dict):
            return {
                str(uid): profile
                for uid, profile in loaded.items()
                if isinstance(profile, dict)
            }

        return {}

    async def init_db(self) -> None:
        if os.path.exists(self.db_file):
            async with aiofiles.open(self.db_file, "r", encoding="utf-8") as file:
                content = await file.read()
                loaded = json.loads(content) if content else {}
                self.data = self._normalize_loaded_memory(loaded)
        else:
            self.data = {}

        if os.path.exists(self.permanent_db_file):
            async with aiofiles.open(self.permanent_db_file, "r", encoding="utf-8") as file:
                content = await file.read()
                self.permanent_data = json.loads(content) if content else {"facts": []}
        else:
            self.permanent_data = {"facts": []}

    async def _save_db(self) -> None:
        async with self.lock:
            async with aiofiles.open(self.db_file, "w", encoding="utf-8") as file:
                await file.write(json.dumps(self.data, ensure_ascii=False, indent=2))

    async def _save_permanent_db(self) -> None:
        async with self.lock:
            async with aiofiles.open(self.permanent_db_file, "w", encoding="utf-8") as file:
                await file.write(json.dumps(self.permanent_data, ensure_ascii=False, indent=2))

    def _ensure_user(self, user_id: str, user_name: str | None = None) -> dict[str, Any]:
        uid = str(user_id)
        if uid not in self.data or not isinstance(self.data[uid], dict):
            self.data[uid] = _default_user_memory(uid, user_name or UNKNOWN)

        profile = self.data[uid]
        default_profile = _default_user_memory(uid, user_name or profile.get("name", UNKNOWN))
        for key, value in default_profile.items():
            profile.setdefault(key, value)

        profile["user_id"] = uid
        if user_name:
            profile["name"] = user_name

        return profile

    def _parse_memory_payload(self, content: str) -> dict[str, Any]:
        stripped = content.strip()
        if not stripped:
            return {}

        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        return {
            "important_events": [
                {
                    "date": _now_date(),
                    "event": stripped,
                    "type": "事實/動態",
                }
            ]
        }

    def get_formatted_memory(self, user_id: str | None = None, user_name: str | None = None) -> str:
        if user_id:
            profile = self.data.get(str(user_id))
        elif user_name:
            profile = next((data for data in self.data.values() if data.get("name") == user_name), None)
        else:
            return "無"

        if not profile:
            return "無"

        return json.dumps(profile, ensure_ascii=False, indent=2)

    def get_all_memory(self) -> str:
        if not self.data:
            return "無"

        profiles = []
        for uid in sorted(self.data):
            profile = self._ensure_user(uid)
            profiles.append(profile)

        return _truncate_context(json.dumps(profiles, ensure_ascii=False, indent=2))

    def get_relevant_memory_context(self, user_ids: set[str] | set[int]) -> str:
        if not self.data:
            return "無"

        relevant_ids = {str(user_id) for user_id in user_ids}
        profiles = []
        for uid in sorted(relevant_ids):
            if uid in self.data:
                profiles.append(self._ensure_user(uid))

        relevant_text = json.dumps(profiles, ensure_ascii=False, indent=2) if profiles else "無"

        index_lines = []
        for uid in sorted(self.data):
            if uid in relevant_ids:
                continue

            profile = self._ensure_user(uid)
            name = profile.get("name", UNKNOWN)
            nickname = profile.get("basic_info", {}).get("nickname", UNKNOWN)
            recent = profile.get("recent_events", UNKNOWN)
            event_count = len(profile.get("important_events", []))
            index_lines.append(
                f"- {name} (ID:{uid})，暱稱:{nickname}，近況:{recent}，重要事件:{event_count}筆"
            )

        index_text = "\n".join(index_lines) if index_lines else "無"
        return _truncate_context(
            "[相關使用者完整記憶]\n"
            f"{relevant_text}\n\n"
            "[其他使用者記憶索引]\n"
            f"{index_text}\n"
            "若需要索引中某人的完整記憶，可使用 [[LOOKUP_MEMORY: DiscordID]]。"
        )

    def get_permanent_memory(self) -> str:
        facts = self.permanent_data.get("facts", [])
        if not facts:
            return "無"

        return "\n".join(f"[{fact['id'][:8]}] {fact['content']}" for fact in facts)

    async def add_memory(self, user_id: str, content: str, user_name: str | None = None) -> None:
        profile = self._ensure_user(user_id, user_name)
        payload = self._parse_memory_payload(content)
        for key, value in payload.items():
            if key == "user_id":
                continue
            profile[key] = _merge_value(profile.get(key), value)

        await self._save_db()

    async def set_profile_field(self, user_id: str, path: str, value: str, user_name: str | None = None) -> bool:
        profile = self._ensure_user(user_id, user_name)
        path = path.strip()
        value = value.strip()
        if not path or not value:
            return False

        allowed_paths = {
            "name",
            "basic_info.nickname",
            "basic_info.location",
            "basic_info.mbti",
            "relationship",
            "recent_events",
        }
        if path not in allowed_paths:
            return False

        current: Any = profile
        parts = path.split(".")
        for part in parts[:-1]:
            if not isinstance(current, dict):
                return False
            current = current.setdefault(part, {})

        if not isinstance(current, dict):
            return False

        current[parts[-1]] = value
        await self._save_db()
        return True

    async def add_hobby(self, user_id: str, hobby: str, user_name: str | None = None) -> bool:
        profile = self._ensure_user(user_id, user_name)
        hobby = hobby.strip()
        if not hobby:
            return False

        if hobby not in profile["hobbies"]:
            profile["hobbies"].append(hobby)
            await self._save_db()

        return True

    async def add_gossip(self, user_id: str, source: str, content: str, user_name: str | None = None) -> bool:
        profile = self._ensure_user(user_id, user_name)
        source = source.strip()
        content = content.strip()
        if not content:
            return False

        gossip = {"from": source or UNKNOWN, "content": content}
        if _dedupe_key(gossip) not in {_dedupe_key(item) for item in profile["gossip_pool"]}:
            profile["gossip_pool"].append(gossip)
            await self._save_db()

        return True

    async def add_important_event(
        self,
        user_id: str,
        event: str,
        event_type: str = "事實/動態",
        date: str | None = None,
        user_name: str | None = None,
        source: str | None = None,
        recorded_at: str | None = None,
    ) -> bool:
        profile = self._ensure_user(user_id, user_name)
        event = event.strip()
        if not event:
            return False

        memory_event = {
            "date": (date or _now_date()).strip(),
            "event": event,
            "type": (event_type or "事實/動態").strip(),
        }
        if source:
            memory_event["source"] = source.strip()
        if recorded_at:
            memory_event["recorded_at"] = recorded_at.strip()

        if _dedupe_key(memory_event) not in {_dedupe_key(item) for item in profile["important_events"]}:
            profile["important_events"].append(memory_event)
            await self._save_db()

        return True

    async def add_permanent_memory(self, content: str) -> None:
        if any(fact.get("content") == content for fact in self.permanent_data["facts"]):
            return

        new_fact = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "content": content,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self.permanent_data["facts"].append(new_fact)
        await self._save_permanent_db()

    async def update_memory(
        self,
        user_id: str,
        user_name: str | None,
        fact_id: str,
        new_content: str,
    ) -> bool:
        await self.add_memory(user_id, new_content, user_name)
        return True

    def _delete_path(self, profile: dict[str, Any], path: str) -> bool:
        path = path.strip()
        if not path:
            return False

        list_item_match = re.fullmatch(r"([\w.]+)\s*:\s*(.+)", path)
        if list_item_match:
            list_path, item_text = list_item_match.groups()
            target = self._get_path(profile, list_path)
            if isinstance(target, list):
                original_len = len(target)
                target[:] = [
                    item for item in target
                    if item_text not in _dedupe_key(item)
                ]
                return len(target) != original_len

        parts = path.split(".")
        current: Any = profile
        for part in parts[:-1]:
            current = self._get_path(current, part)
            if current is None:
                return False

        last = parts[-1]
        index_match = re.fullmatch(r"(\w+)\[(\d+)\]", last)
        if index_match and isinstance(current, dict):
            key, index_text = index_match.groups()
            target_list = current.get(key)
            index = int(index_text)
            if isinstance(target_list, list) and 0 <= index < len(target_list):
                target_list.pop(index)
                return True
            return False

        if isinstance(current, dict) and last in current:
            del current[last]
            return True

        return False

    def _get_path(self, value: Any, path: str) -> Any:
        current = value
        for part in path.split("."):
            index_match = re.fullmatch(r"(\w+)\[(\d+)\]", part)
            if index_match:
                key, index_text = index_match.groups()
                if not isinstance(current, dict):
                    return None
                target_list = current.get(key)
                index = int(index_text)
                if not isinstance(target_list, list) or index >= len(target_list):
                    return None
                current = target_list[index]
            elif isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    async def delete_memory(self, user_id: str, user_name: str | None, fact_id: str) -> bool:
        profile = self._ensure_user(user_id, user_name)
        deleted = self._delete_path(profile, fact_id)
        if deleted:
            await self._save_db()
        return deleted

    async def update_permanent_memory(self, fact_id: str, new_content: str) -> bool:
        for fact in self.permanent_data.get("facts", []):
            if fact["id"].startswith(fact_id):
                fact["content"] = new_content
                fact["time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                await self._save_permanent_db()
                return True

        return False

    async def delete_permanent_memory(self, fact_id: str) -> bool:
        facts = self.permanent_data.get("facts", [])
        for index, fact in enumerate(facts):
            if fact["id"].startswith(fact_id):
                facts.pop(index)
                await self._save_permanent_db()
                return True

        return False
