"""Bounded streaming multipart validation before writing an S3 object."""

import asyncio
import codecs
import hashlib
import unicodedata
from collections.abc import AsyncIterator
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, cast

from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from backend.services.errors import AppError

MIMES = {
    "pdf": {"application/pdf"},
    "txt": {"text/plain"},
    "md": {"text/markdown", "text/x-markdown", "text/plain"},
}


class Upload:
    def __init__(self, max_bytes: int) -> None:
        self.limit = max_bytes
        self.body = cast(BinaryIO, SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b"))
        self.size = 0
        self.name = ""
        self.mime = ""
        self.extension = ""
        self.sha = hashlib.sha256()
        self.headers: dict[bytes, bytes] = {}
        self.header_name = b""
        self.header_value = b""
        self.header_bytes = 0
        self.parts = 0
        self.done = False
        self.part_done = False
        self.prefix = b""
        self.nonblank = False
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")()

    def begin(self) -> None:
        self.parts += 1
        if self.parts != 1:
            raise AppError(422, "UPLOAD_INVALID", "每个上传请求只能包含一个 file 文件。")

    def field(self, data: bytes, start: int, end: int) -> None:
        self.header_bytes += end - start
        if self.header_bytes > 8192:
            raise AppError(422, "UPLOAD_INVALID", "文件头过长。")
        self.header_name += data[start:end].lower()

    def value(self, data: bytes, start: int, end: int) -> None:
        self.header_bytes += end - start
        if self.header_bytes > 8192:
            raise AppError(422, "UPLOAD_INVALID", "文件头过长。")
        self.header_value += data[start:end]

    def header_end(self) -> None:
        if self.header_name in self.headers:
            raise AppError(422, "UPLOAD_INVALID", "文件头重复。")
        self.headers[self.header_name] = self.header_value
        self.header_name = self.header_value = b""

    def headers_done(self) -> None:
        disposition, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or options.get(b"name") != b"file":
            raise AppError(422, "UPLOAD_INVALID", "请通过 file 字段上传文件。")
        try:
            self.name = unicodedata.normalize("NFC", options[b"filename"].decode("utf-8")).strip()
        except (KeyError, UnicodeError):
            raise AppError(422, "UPLOAD_INVALID", "文件名无效。") from None
        if (
            not self.name
            or len(self.name) > 255
            or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in self.name)
        ):
            raise AppError(422, "UPLOAD_INVALID", "文件名无效。")
        self.extension = self.name.rsplit(".", 1)[-1].lower()
        raw_mime, _ = parse_options_header(self.headers.get(b"content-type", b""))
        self.mime = raw_mime.decode("ascii", errors="replace").lower()
        # Browsers sometimes send no MIME or generic octet-stream for .md; inspect content too.
        if self.extension not in MIMES or self.mime not in MIMES[self.extension] | {
            "",
            "application/octet-stream",
        }:
            raise AppError(415, "FILE_TYPE_UNSUPPORTED", "仅支持 PDF、UTF-8 TXT 和 MD 文件。")
        self.mime = {"pdf": "application/pdf", "txt": "text/plain", "md": "text/markdown"}[
            self.extension
        ]

    def data(self, data: bytes, start: int, end: int) -> None:
        block = data[start:end]
        self.size += len(block)
        if self.size > self.limit:
            raise AppError(413, "FILE_TOO_LARGE", "文件超过当前组织的单文件大小限制。")
        if len(self.prefix) < 8:
            self.prefix = (self.prefix + block)[:8]
        if self.extension != "pdf":
            try:
                decoded = self.decoder.decode(block)
                if any(ord(c) < 32 and c not in "\n\r\t\f" for c in decoded):
                    raise UnicodeError
                self.nonblank = self.nonblank or bool(decoded.strip())
            except UnicodeError:
                raise AppError(
                    415, "FILE_TYPE_UNSUPPORTED", "文本文件必须为有效 UTF-8，不能包含二进制内容。"
                ) from None
        self.sha.update(block)
        self.body.write(block)

    def end_part(self) -> None:
        self.part_done = True

    def end(self) -> None:
        self.done = True

    async def read(self, stream: AsyncIterator[bytes], content_type: str) -> None:
        media, options = parse_options_header(content_type)
        boundary = options.get(b"boundary", b"")
        if media != b"multipart/form-data" or not 1 <= len(boundary) <= 200:
            raise AppError(415, "UPLOAD_INVALID", "请使用 multipart/form-data 上传。")
        parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self.begin,
                "on_header_field": self.field,
                "on_header_value": self.value,
                "on_header_end": self.header_end,
                "on_headers_finished": self.headers_done,
                "on_part_data": self.data,
                "on_part_end": self.end_part,
                "on_end": self.end,
            },
        )
        total = 0
        try:
            async with asyncio.timeout(180):
                async for block in stream:
                    total += len(block)
                    if total > self.limit + 65536:
                        raise AppError(413, "FILE_TOO_LARGE", "上传请求超过大小限制。")
                    # Parsing/file writes run on one thread at a time, outside the event loop.
                    task = asyncio.create_task(asyncio.to_thread(parser.write, block))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                        raise
                parser.finalize()
            if not self.done or not self.part_done:
                raise AppError(422, "UPLOAD_INCOMPLETE", "文件传输未完成，请用原提交标识重试。")
            if not self.size:
                raise AppError(422, "FILE_EMPTY", "不能上传空文件。")
            if self.extension == "pdf":
                if not self.prefix.startswith(b"%PDF-"):
                    raise AppError(415, "FILE_TYPE_UNSUPPORTED", "文件内容与 PDF 格式不符。")
            else:
                self.decoder.decode(b"", final=True)
                if not self.nonblank:
                    raise AppError(422, "FILE_EMPTY", "文本没有有效内容。")
        except (MultipartParseError, UnicodeError, ValueError):
            raise AppError(422, "UPLOAD_INVALID", "文件传输格式或文本编码不完整。") from None
        except TimeoutError:
            raise AppError(
                408, "UPLOAD_TIMEOUT", "上传超时，请核对后使用原提交标识重试。"
            ) from None
        self.body.seek(0)
