"""Isolate parser failures from the API and from the Celery task process."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from backend.rag.documents import IngestError


async def parse(body: bytes, mime: str) -> dict[str, Any]:
    with TemporaryDirectory(prefix="eah-parse-") as directory:
        source, output = Path(directory) / "input", Path(directory) / "output.json"
        source.write_bytes(body)
        process = await asyncio.to_thread(
            subprocess.Popen,
            [sys.executable, "-m", "backend.rag.parse_file", str(source), mime, str(output)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        waiter = asyncio.create_task(asyncio.to_thread(process.wait))
        try:
            await asyncio.wait_for(asyncio.shield(waiter), 60)
            if process.returncode != 0 or not output.exists():
                raise IngestError("INGEST_LIMIT_EXCEEDED")
            if output.stat().st_size > 16 * 1024 * 1024:
                raise IngestError("INGEST_LIMIT_EXCEEDED")
            result: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
            if "error" in result:
                raise IngestError(result["error"])
            return result
        except TimeoutError:
            raise IngestError("INGEST_PARSE_TIMEOUT") from None
        finally:
            if process.poll() is None:
                process.kill()
            await waiter
