"""Explicit lifecycle pass; use --apply only after selecting the intended deployment database."""

import argparse
import json

from backend.config import Settings
from backend.db import make_engine
from backend.runtime import run_async
from backend.services.lifecycle import Lifecycle


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply bounded retention cleanup")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--cursor")
    args = parser.parse_args()
    if not args.apply:
        parser.error("No changes made. Select the deployment explicitly, then pass --apply.")
    settings = Settings()
    engine = make_engine(settings)
    try:
        print(json.dumps(await Lifecycle(engine, settings).sweep(args.limit, args.cursor)))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    run_async(main())
