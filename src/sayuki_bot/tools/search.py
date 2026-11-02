from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, urlparse

from .safe_web_proxy import UnsafeWebTarget, public_egress_proxy, validate_public_url


logger = logging.getLogger(__name__)

OBSCURA_BINARY = ContextVar("obscura_binary", default="obscura")
SEARCH_RESULT_LIMIT = 3
OBSCURA_TIMEOUT_SECONDS = 15
MAX_TEXT_PER_PAGE = 6000
MAX_READ_WEB_CHARS = 12000
MAX_OBSCURA_STDOUT_BYTES = 2 * 1024 * 1024
MAX_OBSCURA_STDERR_BYTES = 128 * 1024

_SAFE_SUBPROCESS_ENV_NAMES = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
}


@dataclass(frozen=True)
class BrowserScan:
    query: str
    search_url: str
    scanned_pages: int
    discovered_urls: int
    scanned_characters: int
    raw_text: str


@dataclass(frozen=True)
class WebPageRead:
    url: str
    scanned_characters: int
    text: str
    success: bool


def _search_url(query: str) -> str:
    return f"https://duckduckgo.com/html/?q={quote_plus(query)}"


def _clean_url(raw_url: str) -> str | None:
    stripped = raw_url.strip().strip("()[]<>,.'\"")
    parsed = urlparse(stripped)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    host = parsed.netloc.lower()
    if host.endswith("duckduckgo.com") and parsed.path == "/l/":
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        return target

    if host.endswith("duckduckgo.com"):
        return None

    return stripped


def _extract_urls(text: str, limit: int = SEARCH_RESULT_LIMIT) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    for match in re.finditer(r"https?://[^\s\])}>\"']+", text):
        url = _clean_url(match.group(0))
        if not url or url in seen:
            continue

        seen.add(url)
        urls.append(url)
        if len(urls) >= limit:
            break

    return urls


def _obscura_bin() -> str:
    # web_tools resolves tools.obscura_bin against the deployment root once.
    return OBSCURA_BINARY.get()


def _sanitized_subprocess_environment() -> dict[str, str]:
    """Keep browser runtime settings without forwarding Bot/API credentials."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_SUBPROCESS_ENV_NAMES or key.startswith("LC_")
    }
    environment["NO_PROXY"] = ""
    environment["no_proxy"] = ""
    return environment


async def _terminate_subprocess(proc) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


async def _fetch_with_obscura(url: str, dump: str) -> str:
    safe_url = await validate_public_url(url)
    obscura_bin = _obscura_bin()
    async with public_egress_proxy() as proxy:
        subprocess_env = _sanitized_subprocess_environment()
        proc = await asyncio.create_subprocess_exec(
            obscura_bin,
            "--proxy",
            proxy.url,
            "fetch",
            safe_url,
            "--dump",
            dump,
            "--wait-until",
            "load",
            "--timeout",
            str(OBSCURA_TIMEOUT_SECONDS),
            "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_env,
            cwd=tempfile.gettempdir(),
        )

        async def read_limited(stream, limit: int, label: str) -> bytes:
            chunks = []
            size = 0
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > limit:
                    raise RuntimeError(f"Obscura {label}超過安全大小限制")
                chunks.append(chunk)

        try:
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(
                    read_limited(
                        proc.stdout,
                        MAX_OBSCURA_STDOUT_BYTES,
                        "stdout",
                    ),
                    read_limited(
                        proc.stderr,
                        MAX_OBSCURA_STDERR_BYTES,
                        "stderr",
                    ),
                    proc.wait(),
                ),
                timeout=OBSCURA_TIMEOUT_SECONDS + 5,
            )
        except asyncio.TimeoutError:
            await _terminate_subprocess(proc)
            raise RuntimeError("Obscura執行逾時")
        except BaseException:
            await _terminate_subprocess(proc)
            raise

    if proc.returncode != 0:
        error = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(error or f"obscura fetch failed with exit code {proc.returncode}")

    return stdout.decode("utf-8", errors="replace").strip()


async def search_web(query: str) -> BrowserScan:
    search_url = _search_url(query)

    try:
        search_tasks = [
            asyncio.create_task(_fetch_with_obscura(search_url, "text")),
            asyncio.create_task(_fetch_with_obscura(search_url, "links")),
        ]
        try:
            search_text, search_links = await asyncio.gather(*search_tasks)
        except BaseException:
            for task in search_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*search_tasks, return_exceptions=True)
            raise

        result_urls = _extract_urls(search_links) or _extract_urls(search_text)
        page_texts = [f"【搜尋頁】\n{search_text[:MAX_TEXT_PER_PAGE]}"]

        for index, url in enumerate(result_urls, start=1):
            try:
                page_text = await _fetch_with_obscura(url, "text")
            except Exception as exc:
                logger.warning("Obscura 掃描結果頁失敗: %s (%s)", url, exc)
                page_text = f"掃描失敗: {exc}"

            page_texts.append(f"【結果頁 {index}: {url}】\n{page_text[:MAX_TEXT_PER_PAGE]}")

        raw_text = "\n\n".join(page_texts).strip()
        return BrowserScan(
            query=query,
            search_url=search_url,
            scanned_pages=1 + len(result_urls),
            discovered_urls=len(result_urls),
            scanned_characters=len(raw_text),
            raw_text=raw_text,
        )
    except FileNotFoundError:
        return BrowserScan(
            query=query,
            search_url=search_url,
            scanned_pages=0,
            discovered_urls=0,
            scanned_characters=0,
            raw_text=(
                "搜尋功能未啟用：找不到 Obscura 可執行檔。"
                "請安裝 https://github.com/h4ckf0r0day/obscura.git 的 release binary，"
                "並放入 PATH，或在配置檔以 tools.obscura_bin 指定路徑。"
            ),
        )
    except UnsafeWebTarget as exc:
        return BrowserScan(
            query=query,
            search_url=search_url,
            scanned_pages=0,
            discovered_urls=0,
            scanned_characters=0,
            raw_text=f"搜尋目標被公開網路安全政策拒絕：{exc}",
        )
    except Exception as exc:
        return BrowserScan(
            query=query,
            search_url=search_url,
            scanned_pages=0,
            discovered_urls=0,
            scanned_characters=0,
            raw_text=f"搜尋發生錯誤: {exc}",
        )


async def read_web_page(url: str) -> WebPageRead:
    clean_url = _clean_url(url)
    if not clean_url:
        return WebPageRead(
            url=url,
            scanned_characters=0,
            text="讀取失敗：URL格式不正確，只支援http/https網址。",
            success=False,
        )

    try:
        page_text = await _fetch_with_obscura(clean_url, "text")
        text = page_text[:MAX_READ_WEB_CHARS]
        if len(page_text) > MAX_READ_WEB_CHARS:
            text += "\n...（網頁文字過長，已截斷）"
        return WebPageRead(
            url=clean_url,
            scanned_characters=len(page_text),
            text=text or "讀取成功，但網頁沒有可用文字內容。",
            success=True,
        )
    except FileNotFoundError:
        return WebPageRead(
            url=clean_url,
            scanned_characters=0,
            text=(
                "讀取網頁功能未啟用：找不到 Obscura 可執行檔。"
                "請安裝 https://github.com/h4ckf0r0day/obscura.git 的 release binary，"
                "並放入 PATH，或在配置檔以 tools.obscura_bin 指定路徑。"
            ),
            success=False,
        )
    except UnsafeWebTarget as exc:
        return WebPageRead(
            url=clean_url,
            scanned_characters=0,
            text=f"讀取失敗：網址被公開網路安全政策拒絕：{exc}",
            success=False,
        )
    except Exception as exc:
        return WebPageRead(
            url=clean_url,
            scanned_characters=0,
            text=f"讀取網頁失敗：{exc}",
            success=False,
        )
