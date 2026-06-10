from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

from openai import AsyncOpenAI

import math_tools

from .config import TW_TZ
from .search import BrowserScan, search_web
from .tool_tags import MEMORY_TOOL_NAMES, find_balanced_tool_tags


logger = logging.getLogger(__name__)


def _collect_memory_side_effect_tags(reply: str) -> list[str]:
    return [tag.raw for tag in find_balanced_tool_tags(reply, MEMORY_TOOL_NAMES)]


def _message_without_match(reply: str, match: re.Match[str]) -> str:
    text_before = reply[:match.start()].strip()
    text_after = reply[match.end():].strip()
    return f"{text_before}\n{text_after}".strip()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get("type") == "image_url":
                image_url = item.get("image_url", {})
                if isinstance(image_url, dict):
                    parts.append(f"[image_url:{image_url.get('url', '')}]")
                else:
                    parts.append("[image_url]")
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _messages_for_log(messages: list) -> tuple[list[dict[str, Any]], int, int]:
    sanitized = []
    system_chars = 0
    input_chars = 0
    for message in messages:
        role = message.get("role", "")
        content_text = _content_text(message.get("content", ""))
        if role == "system":
            system_chars += len(content_text)
            sanitized.append({"role": role, "system_prompt_omitted": True, "chars": len(content_text)})
        else:
            input_chars += len(content_text)
            sanitized.append({"role": role, "content": content_text, "chars": len(content_text)})
    return sanitized, input_chars, system_chars


class OpenRouterLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        small_model: str,
        vl_model: str,
        tool_stats_mgr=None,
        invocation_logger=None,
        use_reasoning_effort: bool = False,
        reasoning_effort: str = "medium",
    ):
        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "https://github.com/SayukiBot",
                "X-Title": "Sayuki Discord Bot",
            },
        )
        self.model = model
        self.small_model = small_model or model
        self.vl_model = vl_model
        self.tool_stats_mgr = tool_stats_mgr
        self.invocation_logger = invocation_logger
        self.use_reasoning_effort = use_reasoning_effort
        self.reasoning_effort = reasoning_effort.strip().lower()

    def _text_reasoning_kwargs(self) -> dict:
        if not self.use_reasoning_effort or not self.reasoning_effort:
            return {}

        return {"extra_body": {"reasoning": {"effort": self.reasoning_effort}}}

    async def _log_invocation(
        self,
        call_type: str,
        model: str,
        messages: list,
        max_tokens: int,
        temperature: float,
        started_at: str,
        duration_ms: float,
        output: str = "",
        success: bool = True,
        error: str = "",
    ) -> None:
        if not self.invocation_logger:
            return

        sanitized_messages, input_chars, system_chars = _messages_for_log(messages)
        await self.invocation_logger.write(
            {
                "log_type": "llm_call",
                "time": started_at,
                "call_type": call_type,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "success": success,
                "duration_ms": round(duration_ms, 2),
                "input_chars_without_system": input_chars,
                "system_prompt_chars": system_chars,
                "output_chars": len(output or ""),
                "messages": sanitized_messages,
                "output": output or "",
                "error": error,
            }
        )

    async def describe_image_async(self, image_url: str) -> str:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "你是一個精確的視覺解析器，請客觀、詳細地描述這張圖片的內容。如果有文字請完整提取。不要加入任何對話用語或個人見解。",
                    },
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ]

        try:
            logger.info("正在呼叫 VL 模型解析圖片...")
            started_at = datetime.now(TW_TZ).isoformat(timespec="seconds")
            started = time.perf_counter()
            resp = await self.client.chat.completions.create(
                model=self.vl_model,
                messages=messages,
                max_tokens=1024,
                temperature=0.1,
            )
            desc = resp.choices[0].message.content
            output = desc if desc else "無法解析圖片內容。"
            await self._log_invocation(
                "view_image_vl",
                self.vl_model,
                messages,
                1024,
                0.1,
                started_at,
                (time.perf_counter() - started) * 1000,
                output,
                True,
            )
            return output
        except Exception as exc:
            logger.error("VL 模型解析失敗: %s", exc)
            await self._log_invocation(
                "view_image_vl",
                self.vl_model,
                messages,
                1024,
                0.1,
                datetime.now(TW_TZ).isoformat(timespec="seconds"),
                0.0,
                "",
                False,
                str(exc),
            )
            return f"圖片解析失敗 ({exc})"

    async def _process_browser_scan_async(self, scan: BrowserScan) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一個資料處理器。請只根據提供的瀏覽器掃描資料，"
                    "整理出目標查詢需要的核心資訊。使用繁體中文，客觀、精簡，"
                    "保留重要事實、數字、日期與來源線索；不要模仿角色語氣。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"目標查詢：{scan.query}\n"
                    f"瀏覽器掃描資料量：掃描頁面 {scan.scanned_pages} 頁，"
                    f"找到候選網址 {scan.discovered_urls} 個，"
                    f"原始文字 {scan.scanned_characters} 字元。\n\n"
                    f"【瀏覽器掃描資料】\n{scan.raw_text}"
                ),
            },
        ]

        try:
            started_at = datetime.now(TW_TZ).isoformat(timespec="seconds")
            started = time.perf_counter()
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=2048,
                temperature=0.1,
                **self._text_reasoning_kwargs(),
            )
            processed = resp.choices[0].message.content
            output = processed.strip() if processed else "資料處理器沒有整理出可用內容。"
            await self._log_invocation(
                "search_processor",
                self.model,
                messages,
                2048,
                0.1,
                started_at,
                (time.perf_counter() - started) * 1000,
                output,
                True,
            )
            return output
        except Exception as exc:
            logger.error("搜尋資料處理器錯誤: %s", exc)
            await self._log_invocation(
                "search_processor",
                self.model,
                messages,
                2048,
                0.1,
                datetime.now(TW_TZ).isoformat(timespec="seconds"),
                0.0,
                "",
                False,
                str(exc),
            )
            return f"資料處理器整理失敗：{exc}\n\n{scan.raw_text[:2000]}"

    async def summarize_async(
        self,
        prompt: str,
        max_tokens: int = 300,
        use_reasoning_effort: bool = False,
        call_type: str = "summary",
    ) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一個短期記憶壓縮器。請客觀整理提供的聊天內容，"
                    "保留有助於後續對話理解的重點，不要模仿角色語氣。"
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            started_at = datetime.now(TW_TZ).isoformat(timespec="seconds")
            started = time.perf_counter()
            resp = await self.client.chat.completions.create(
                model=self.small_model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.1,
                **(self._text_reasoning_kwargs() if use_reasoning_effort else {}),
            )
            summary = resp.choices[0].message.content
            output = summary.strip() if summary else "無"
            await self._log_invocation(
                call_type,
                self.small_model,
                messages,
                max_tokens,
                0.1,
                started_at,
                (time.perf_counter() - started) * 1000,
                output,
                True,
            )
            return output
        except Exception as exc:
            logger.error("短期記憶壓縮失敗: %s", exc)
            await self._log_invocation(
                call_type,
                self.small_model,
                messages,
                max_tokens,
                0.1,
                datetime.now(TW_TZ).isoformat(timespec="seconds"),
                0.0,
                "",
                False,
                str(exc),
            )
            return "短期記憶壓縮失敗。"

    async def generate_async(self, messages: list, max_search: int = 2, call_type: str = "main") -> str:
        for iteration in range(max_search):
            try:
                started_at = datetime.now(TW_TZ).isoformat(timespec="seconds")
                started = time.perf_counter()
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=8192,
                    temperature=0.65,
                    **self._text_reasoning_kwargs(),
                )
                raw_content = resp.choices[0].message.content

                if not raw_content:
                    finish_reason = resp.choices[0].finish_reason
                    logger.warning("OpenRouter 回傳空字串！中斷原因: %s", finish_reason)
                    reply = "(⁠｡⁠•́⁠︿⁠•̀⁠｡⁠)  抱歉，我剛剛說太多了，頭有點暈了。"
                else:
                    reply = raw_content

                await self._log_invocation(
                    f"{call_type}:iteration_{iteration + 1}",
                    self.model,
                    messages,
                    8192,
                    0.65,
                    started_at,
                    (time.perf_counter() - started) * 1000,
                    reply,
                    True,
                )

                calc_match = re.search(r"\[\[MATH_CALC:\s*(.*?)\s*\]\]", reply)
                if calc_match:
                    expr = calc_match.group(1).strip()
                    logger.info("執行數學計算: %s", expr)
                    started = time.perf_counter()
                    result = await math_tools.MathToolkit.calculate(expr)
                    if self.tool_stats_mgr:
                        await self.tool_stats_mgr.record_tool(
                            "MATH_CALC",
                            (time.perf_counter() - started) * 1000,
                            not result.startswith("計算錯誤"),
                        )

                    content_to_keep = _message_without_match(reply, calc_match)
                    if content_to_keep:
                        messages.append({"role": "assistant", "content": content_to_keep})

                    messages.append(
                        {
                            "role": "user",
                            "content": f"【數學計算結果】\n{expr} = {result}\n請根據結果自然地回答使用者。",
                        }
                    )

                    memory_tags = _collect_memory_side_effect_tags(reply)
                    if memory_tags:
                        messages.append(
                            {
                                "role": "user",
                                "content": "【請一併處理以下操作】\n" + "\n".join(memory_tags),
                            }
                        )

                    continue

                search_match = re.search(r"\[\[SEARCH:\s*(.*?)\]\]", reply)
                if search_match:
                    query = search_match.group(1).strip()
                    logger.info("執行搜尋: %s", query)

                    content_to_keep = _message_without_match(reply, search_match)
                    if content_to_keep:
                        messages.append({"role": "assistant", "content": content_to_keep})

                    started = time.perf_counter()
                    scan = await search_web(query)
                    processed_result = await self._process_browser_scan_async(scan)
                    if self.tool_stats_mgr:
                        search_success = (
                            scan.scanned_pages > 0
                            and not scan.raw_text.startswith("搜尋功能未啟用")
                            and not scan.raw_text.startswith("搜尋發生錯誤")
                            and not processed_result.startswith("資料處理器整理失敗")
                        )
                        await self.tool_stats_mgr.record_tool(
                            "SEARCH",
                            (time.perf_counter() - started) * 1000,
                            search_success,
                        )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "【系統搜尋結果】\n"
                                f"紗月要查：{scan.query}\n"
                                f"Obscura 瀏覽器掃描資料量：掃描頁面 {scan.scanned_pages} 頁，"
                                f"找到候選網址 {scan.discovered_urls} 個，"
                                f"整理前原始文字 {scan.scanned_characters} 字元。\n"
                                f"搜尋頁：{scan.search_url}\n\n"
                                "【資料處理器整理結果】\n"
                                f"{processed_result}\n\n"
                                "請根據整理結果自然地回答使用者。"
                            ),
                        }
                    )

                    memory_tags = _collect_memory_side_effect_tags(reply)
                    if memory_tags:
                        messages.append(
                            {
                                "role": "user",
                                "content": "【請一併處理以下操作】\n" + "\n".join(memory_tags),
                            }
                        )

                    continue

                return reply
            except Exception as exc:
                logger.error("LLM 錯誤: %s", exc)
                await self._log_invocation(
                    f"{call_type}:iteration_{iteration + 1}",
                    self.model,
                    messages,
                    8192,
                    0.65,
                    datetime.now(TW_TZ).isoformat(timespec="seconds"),
                    0.0,
                    "",
                    False,
                    str(exc),
                )
                if iteration == max_search - 1:
                    return "...抱歉，我剛剛恍神了。"

        return "嗚... 查了好多資料，頭有點暈了。"
