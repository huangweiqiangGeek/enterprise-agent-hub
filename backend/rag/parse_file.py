"""Private parser child entry; resource limits apply before reading untrusted file content."""

import json
import sys
from pathlib import Path

from backend.rag.documents import IngestError, extract


def main() -> None:
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    try:
        result = extract(Path(sys.argv[1]).read_bytes(), sys.argv[2])
    except IngestError as exc:
        result = {"error": exc.code}
    except MemoryError:
        result = {"error": "INGEST_LIMIT_EXCEEDED"}
    Path(sys.argv[3]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
