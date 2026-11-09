"""Parser outcomes and source locators are deterministic and independent of models."""

from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from backend.rag.documents import IngestError, extract, lexemes, split
from backend.rag.parser import parse


def pdf(*texts: str, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 250 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = stream
    if encrypted:
        writer.encrypt("test-only")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize(
    ("body", "mime", "code"),
    [
        (b"%PDF-1.7\nbroken", "application/pdf", "PDF_INVALID"),
        (pdf(""), "application/pdf", "DOCUMENT_NO_TEXT"),
        (pdf("secret", encrypted=True), "application/pdf", "PDF_ENCRYPTED"),
        (b"\xff", "text/plain", "DOCUMENT_ENCODING_INVALID"),
        (b" \n", "text/plain", "DOCUMENT_NO_TEXT"),
    ],
)
def test_explicit_parse_failures(body: bytes, mime: str, code: str) -> None:
    with pytest.raises(IngestError, match=code):
        extract(body, mime)


def test_pdf_pages_and_unicode_offsets() -> None:
    document = extract(pdf("Page one", "Page two"), "application/pdf")
    chunks = split(document)
    assert [c.locator["pageStart"] for c in chunks] == [1, 2]
    text = "# 标题\n\n" + "中文English😀" * 150 + "\n\n## 第二节\n内容"
    document = extract(b"\xef\xbb\xbf" + text.encode(), "text/markdown")
    chunks = split(document)
    assert len(chunks) > 3 and document["text"] == text
    for chunk in chunks:
        assert len(chunk.text.encode()) <= 384
        assert text[chunk.locator["textStart"] : chunk.locator["textEnd"]] == chunk.text
        assert "pageStart" not in chunk.locator and len(chunk.digest) == 64
    assert chunks[-1].locator["headingPath"] == ["标题", "第二节"]
    assert {"中文", "文", "english"} <= set(lexemes(text).split())


async def test_parser_child_isolation() -> None:
    parsed = await parse(pdf("Isolated text"), "application/pdf")
    assert "Isolated text" in parsed["text"]
    with pytest.raises(IngestError, match="PDF_INVALID"):
        await parse(b"%PDF-1.7\nbroken", "application/pdf")
