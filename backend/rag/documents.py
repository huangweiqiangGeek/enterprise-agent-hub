"""Deterministic text extraction and Unicode-safe, conservatively bounded chunks."""

import hashlib
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from pypdf import PdfReader

PIPELINE = "utf8-bytes-384-overlap-48-v1"
MAX_TEXT = 2_000_000
MAX_CHUNKS = 4096


class IngestError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class Chunk:
    text: str
    locator: dict[str, Any]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def extract(body: bytes, mime: str) -> dict[str, Any]:
    pages: list[str] = []
    if mime == "application/pdf":
        try:
            reader = PdfReader(BytesIO(body), strict=True)
            if reader.is_encrypted:
                raise IngestError("PDF_ENCRYPTED")
            if len(reader.pages) > 500:
                raise IngestError("INGEST_LIMIT_EXCEEDED")
            total = 0
            for page in reader.pages:
                value = page.extract_text() or ""
                total += len(value)
                if total > MAX_TEXT:
                    raise IngestError("INGEST_LIMIT_EXCEEDED")
                pages.append(value)
        except IngestError:
            raise
        except MemoryError:
            raise IngestError("INGEST_LIMIT_EXCEEDED") from None
        except Exception:
            raise IngestError("PDF_INVALID") from None
    else:
        try:
            pages = [body.decode("utf-8-sig")]
        except UnicodeDecodeError:
            raise IngestError("DOCUMENT_ENCODING_INVALID") from None
    text = "\n\n".join(pages)
    if len(text) > MAX_TEXT or "\x00" in text:
        raise IngestError("INGEST_LIMIT_EXCEEDED")
    if not text.strip():
        raise IngestError("DOCUMENT_NO_TEXT")
    spans: list[dict[str, Any]] = []
    offset, paragraph = 0, 0
    headings: list[str] = []
    for page_no, page_text in enumerate(pages, 1):
        for match in re.finditer(r"[^\r\n]+(?:\r?\n(?!\r?\n|#{1,6}\s)[^\r\n]+)*", page_text):
            value = match.group()
            if not value.strip():
                continue
            paragraph += 1
            heading = re.match(r"^(#{1,6})\s+([^\r\n]+)", value)
            if mime != "application/pdf" and heading:
                level = len(heading[1])
                headings = headings[: level - 1] + [heading[2].strip()]
            locator: dict[str, Any] = {
                "kind": "pdf" if mime == "application/pdf" else "text",
                "paragraphStart": paragraph,
                "paragraphEnd": paragraph,
            }
            if mime == "application/pdf":
                locator.update(pageStart=page_no, pageEnd=page_no)
            else:
                locator["headingPath"] = list(headings)
            spans.append({"start": offset + match.start(), "end": offset + match.end(), **locator})
        offset += len(page_text) + 2
    return {"text": text, "spans": spans}


def split(document: dict[str, Any]) -> list[Chunk]:
    text: str = document["text"]
    chunks = []
    for span in document["spans"]:
        start, end = span["start"], span["end"]
        while start < end:
            stop, used = start, 0
            while stop < end and used + len(text[stop].encode()) <= 384:
                used += len(text[stop].encode())
                stop += 1
            value = text[start:stop]
            if value.strip():
                locator = {k: v for k, v in span.items() if k not in {"start", "end"}}
                locator.update(textStart=start, textEnd=stop)
                chunks.append(Chunk(value, locator))
            if len(chunks) > MAX_CHUNKS:
                raise IngestError("INGEST_LIMIT_EXCEEDED")
            if stop == end:
                break
            overlap, used = stop, 0
            while overlap > start and used + len(text[overlap - 1].encode()) <= 48:
                overlap -= 1
                used += len(text[overlap].encode())
            start = overlap
    if not chunks:
        raise IngestError("DOCUMENT_NO_TEXT")
    return chunks


def lexemes(text: str) -> str:
    terms: list[str] = []
    for value in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9]+", text):
        if "\u3400" <= value[0] <= "\u9fff":
            terms.extend(value)
            terms.extend(value[i : i + 2] for i in range(len(value) - 1))
        else:
            terms.append(value.lower())
    return " ".join(dict.fromkeys(terms))
