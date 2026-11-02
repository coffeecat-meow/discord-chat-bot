from __future__ import annotations

import base64
import binascii
import hashlib
import re
from urllib.parse import quote, unquote


MAX_TRANSFORM_INPUT_CHARS = 4000
MAX_TRANSFORM_OUTPUT_CHARS = 12000
TEXT_TRANSFORM_OPERATIONS = (
    "base64_encode",
    "base64_decode",
    "base32_encode",
    "base32_decode",
    "hex_encode",
    "hex_decode",
    "url_encode",
    "url_decode",
    "md5",
    "sha1",
    "sha256",
    "sha512",
)


def _validate_input(operation: str, text: str) -> tuple[str, str]:
    normalized_operation = str(operation or "").strip().lower()
    if normalized_operation not in TEXT_TRANSFORM_OPERATIONS:
        raise ValueError(f"不支援的轉換操作：{normalized_operation or '空白'}")
    if not isinstance(text, str):
        raise ValueError("text必須是字串")
    if len(text) > MAX_TRANSFORM_INPUT_CHARS:
        raise ValueError(f"text不可超過{MAX_TRANSFORM_INPUT_CHARS}個字元")
    return normalized_operation, text


def _decode_utf8(payload: bytes, operation: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{operation}的結果不是UTF-8文字；此工具不輸出任意二進位資料") from None


def _bounded_output(result: str) -> str:
    if len(result) > MAX_TRANSFORM_OUTPUT_CHARS:
        raise ValueError(f"轉換結果不可超過{MAX_TRANSFORM_OUTPUT_CHARS}個字元")
    return result


def transform_text(operation: str, text: str) -> str:
    operation, text = _validate_input(operation, text)
    utf8 = text.encode("utf-8")

    if operation == "base64_encode":
        result = base64.b64encode(utf8).decode("ascii")
    elif operation == "base64_decode":
        compact = "".join(text.split())
        try:
            encoded = compact.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("base64輸入只能包含ASCII字元") from None
        encoded += b"=" * (-len(encoded) % 4)
        try:
            result = _decode_utf8(
                base64.b64decode(encoded, validate=True),
                operation,
            )
        except (binascii.Error, ValueError) as exc:
            if isinstance(exc, ValueError) and "UTF-8" in str(exc):
                raise
            raise ValueError("base64格式無效") from None
    elif operation == "base32_encode":
        result = base64.b32encode(utf8).decode("ascii")
    elif operation == "base32_decode":
        compact = "".join(text.split()).upper()
        try:
            encoded = compact.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("base32輸入只能包含ASCII字元") from None
        encoded += b"=" * (-len(encoded) % 8)
        try:
            result = _decode_utf8(
                base64.b32decode(encoded, casefold=True),
                operation,
            )
        except (binascii.Error, ValueError) as exc:
            if isinstance(exc, ValueError) and "UTF-8" in str(exc):
                raise
            raise ValueError("base32格式無效") from None
    elif operation == "hex_encode":
        result = utf8.hex()
    elif operation == "hex_decode":
        compact = "".join(text.split())
        try:
            result = _decode_utf8(bytes.fromhex(compact), operation)
        except ValueError as exc:
            if "UTF-8" in str(exc):
                raise
            raise ValueError("hex格式無效") from None
    elif operation == "url_encode":
        result = quote(text, safe="")
    elif operation == "url_decode":
        if re.search(r"%(?![0-9A-Fa-f]{2})", text):
            raise ValueError("URL percent-encoding格式無效")
        result = unquote(text, errors="strict")
    elif operation in {"md5", "sha1", "sha256", "sha512"}:
        result = hashlib.new(operation, utf8).hexdigest()
    else:
        raise AssertionError(f"unhandled operation: {operation}")

    return _bounded_output(result)
