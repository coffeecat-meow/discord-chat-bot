from __future__ import annotations

import logging
import re
import time

from openai import AsyncOpenAI

import math_tools

from .search import BrowserScan, search_web


logger = logging.getLogger(__name__)

MEMORY_SIDE_EFFECT_PATTERNS = [
    r"\[\[MEM_SET:.*?\]\]",
    r"\[\[MEM_HOBBY:.*?\]\]",
    r"\[\[MEM_GOSSIP:.*?\]\]",
    r"\[\[MEM_EVENT:.*?\]\]",
    r"\[\[MEM_EVENT_FOR:.*?\]\]",
    r"\[\[MEMORY:.*?\]\]",
    r"\[\[EDIT_MEMORY:.*?\]\]",
    r"\[\[DELETE_MEMORY:.*?\]\]",
    r"\[\[PERMANENT_MEMORY:.*?\]\]",
    r"\[\[EDIT_PERMANENT_MEMORY:.*?\]\]",
    r"\[\[DELETE_PERMANENT_MEMORY:.*?\]\]",
]


def _collect_memory_side_effect_tags(reply: str) -> list[str]:
    tags: list[str] = []
    for pattern in MEMORY_SIDE_EFFECT_PATTERNS:
        tags.extend(re.findall(pattern, reply))
    return tags


def _message_without_match(reply: str, match: re.Match[str]) -> str:
    text_before = reply[:match.start()].strip()
    text_after = reply[match.end():].strip()
    return f"{text_before}\n{text_after}".strip()


class OpenRouterLLM:
    def __init__(self, api_key: str, model: str, vl_model: str, tool_stats_mgr=None):
        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "https://github.com/SayukiBot",
                "X-Title": "Sayuki Discord Bot",
            },
        )
        self.model = model
        self.vl_model = vl_model
        self.tool_stats_mgr = tool_stats_mgr

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
            resp = await self.client.chat.completions.create(
                model=self.vl_model,
                messages=messages,
                max_tokens=1024,
                temperature=0.1,
            )
            desc = resp.choices[0].message.content
            return desc if desc else "無法解析圖片內容。"
        except Exception as exc:
            logger.error("VL 模型解析失敗: %s", exc)
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
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=2048,
                temperature=0.1,
            )
            processed = resp.choices[0].message.content
            return processed.strip() if processed else "資料處理器沒有整理出可用內容。"
        except Exception as exc:
            logger.error("搜尋資料處理器錯誤: %s", exc)
            return f"資料處理器整理失敗：{exc}\n\n{scan.raw_text[:2000]}"

    async def summarize_async(self, prompt: str, max_tokens: int = 900) -> str:
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
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.1,
            )
            summary = resp.choices[0].message.content
            return summary.strip() if summary else "無"
        except Exception as exc:
            logger.error("短期記憶壓縮失敗: %s", exc)
            return "短期記憶壓縮失敗。"

    async def generate_async(self, messages: list, max_search: int = 2) -> str:
        for iteration in range(max_search):
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=8192,
                    temperature=0.65,
                )
                raw_content = resp.choices[0].message.content

                if not raw_content:
                    finish_reason = resp.choices[0].finish_reason
                    logger.warning("OpenRouter 回傳空字串！中斷原因: %s", finish_reason)
                    reply = "(⁠｡⁠•́⁠︿⁠•̀⁠｡⁠)  抱歉，我剛剛說太多了，頭有點暈了。"
                else:
                    reply = raw_content

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
                if iteration == max_search - 1:
                    return "...抱歉，我剛剛恍神了。"

        return "嗚... 查了好多資料，頭有點暈了。"
