from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit


ALLOWED_WEB_PORTS = {80, 443}
MAX_PROXY_HEADER_BYTES = 64 * 1024
PROXY_IO_TIMEOUT_SECONDS = 20


class UnsafeWebTarget(ValueError):
    pass


def _normalized_host(host: str) -> str:
    value = host.strip().rstrip(".")
    if not value:
        raise UnsafeWebTarget("網址缺少主機名稱")
    try:
        return value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeWebTarget("網址主機名稱無效") from exc


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


async def resolve_public_addresses(host: str, port: int) -> list[str]:
    normalized = _normalized_host(host)
    if normalized == "localhost" or normalized.endswith(".localhost"):
        raise UnsafeWebTarget("禁止存取本機或內部網路")
    if port not in ALLOWED_WEB_PORTS:
        raise UnsafeWebTarget("只允許HTTP/HTTPS標準連接埠")

    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeWebTarget("禁止存取非公開IP位址")
        return [str(literal)]

    loop = asyncio.get_running_loop()
    try:
        results = await loop.getaddrinfo(
            normalized,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafeWebTarget("網址主機無法解析") from exc

    addresses = list(dict.fromkeys(item[4][0] for item in results))
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeWebTarget("網址解析到非公開IP，已拒絕存取")
    return addresses


async def validate_public_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeWebTarget("只支援公開的http/https網址")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeWebTarget("禁止在網址中夾帶帳號或密碼")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeWebTarget("網址連接埠無效") from exc
    await resolve_public_addresses(parsed.hostname, port)
    return parsed.geturl()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(64 * 1024)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, RuntimeError):
            pass


async def _read_proxy_head(reader: asyncio.StreamReader) -> bytes:
    try:
        data = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=PROXY_IO_TIMEOUT_SECONDS,
        )
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError) as exc:
        raise UnsafeWebTarget("proxy請求標頭無效") from exc
    if len(data) > MAX_PROXY_HEADER_BYTES:
        raise UnsafeWebTarget("proxy請求標頭過大")
    return data


def _parse_authority(authority: str, default_port: int) -> tuple[str, int]:
    parsed = urlsplit(f"//{authority}")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeWebTarget("proxy目的地主機無效")
    try:
        return parsed.hostname, parsed.port or default_port
    except ValueError as exc:
        raise UnsafeWebTarget("proxy目的地連接埠無效") from exc


def _rewrite_http_request(head: bytes, path: str, authority: str) -> bytes:
    lines = head.split(b"\r\n")
    method, _, version = lines[0].split(b" ", 2)
    output = [b" ".join((method, path.encode("ascii", errors="ignore") or b"/", version))]
    for line in lines[1:]:
        lower = line.lower()
        if lower.startswith((b"proxy-connection:", b"connection:", b"host:")):
            continue
        output.append(line)
    output.insert(1, f"Host: {authority}".encode("ascii"))
    return b"\r\n".join(output)


@dataclass
class PublicEgressProxy:
    server: asyncio.AbstractServer
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


async def _handle_proxy_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    upstream_writer = None
    try:
        head = await _read_proxy_head(reader)
        request_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="strict")
        method, target, _ = request_line.split(" ", 2)

        if method.upper() == "CONNECT":
            host, port = _parse_authority(target, 443)
            addresses = await resolve_public_addresses(host, port)
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], port),
                timeout=PROXY_IO_TIMEOUT_SECONDS,
            )
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            parsed = urlsplit(target)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise UnsafeWebTarget("proxy只接受absolute-form HTTP網址")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = await resolve_public_addresses(parsed.hostname, port)
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], port),
                timeout=PROXY_IO_TIMEOUT_SECONDS,
            )
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            upstream_writer.write(_rewrite_http_request(head, path, parsed.netloc))
            await upstream_writer.drain()

        await asyncio.gather(
            _pipe(reader, upstream_writer),
            _pipe(upstream_reader, writer),
        )
        return
    except (UnsafeWebTarget, ValueError, UnicodeError, ConnectionError, asyncio.TimeoutError):
        try:
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\n"
                b"Content-Type: text/plain\r\n"
                b"Connection: close\r\n\r\n"
                b"Blocked by public-only web policy."
            )
            await writer.drain()
        except ConnectionError:
            pass
    finally:
        if upstream_writer is not None and not upstream_writer.is_closing():
            upstream_writer.close()
        if not writer.is_closing():
            writer.close()


@asynccontextmanager
async def public_egress_proxy():
    server = await asyncio.start_server(
        _handle_proxy_client,
        host="127.0.0.1",
        port=0,
        limit=MAX_PROXY_HEADER_BYTES,
    )
    socket_info = server.sockets[0].getsockname()
    proxy = PublicEgressProxy(server, str(socket_info[0]), int(socket_info[1]))
    try:
        yield proxy
    finally:
        await proxy.close()
