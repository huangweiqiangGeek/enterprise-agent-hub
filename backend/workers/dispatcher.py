"""Periodically reconcile authoritative PG state and deliver its outbox."""

import asyncio
import json
import sys
from time import monotonic

from backend.config import Settings
from backend.db import make_engine
from backend.runtime import run_async
from backend.services.ingestion import Ingestion
from backend.services.storage import Storage
from backend.workers.outbox import Outbox


async def main() -> None:
    settings = Settings()
    engine = make_engine(settings)
    storage = Storage(settings)
    worker = Ingestion(engine, settings, storage)
    outbox = Outbox(engine, settings, storage)
    cleanup_at, cursor = 0.0, None
    try:
        while True:
            try:
                await worker.reconcile()
                await outbox.dispatch()
                if monotonic() >= cleanup_at:
                    page = await outbox.clean_orphans(cursor)
                    cursor = page["nextCursor"]
                    cleanup_at = monotonic() + (5 if cursor else 3600)
            except Exception:
                print(json.dumps({"errorCode": "DISPATCHER_UNAVAILABLE"}), flush=True)
            if "--once" in sys.argv:
                break
            await asyncio.sleep(5)
    finally:
        outbox.broker.close()
        await engine.dispose()


if __name__ == "__main__":
    run_async(main())
