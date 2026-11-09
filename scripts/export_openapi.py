import argparse
import json
from pathlib import Path

from backend.api.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
    content = json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            raise SystemExit("OpenAPI drift: run uv run python -m scripts.export_openapi")
    else:
        target.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
