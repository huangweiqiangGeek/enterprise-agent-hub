"""Explicit D03 real-provider probe. No credentials means exit 2, never fixture success."""

import json
import os

from dotenv import load_dotenv
from pydantic import ValidationError

from backend.config import Settings
from backend.model_schemas import ModelConfigWrite, ModelKind
from backend.runtime import run_async
from backend.services.errors import AppError
from backend.services.model_probe import PROTOCOLS, Attempt, ModelProbe


async def main() -> int:
    load_dotenv()
    probe = ModelProbe(Settings())
    exit_code = 0
    kinds: tuple[ModelKind, ...] = ("chat", "embedding", "rerank")
    for kind in kinds:
        prefix = "EAH_PROBE_RERANK" if kind == "rerank" else "EAH_PROBE"
        names = [f"{prefix}_API_KEY", f"{prefix}_BASE_URL", f"EAH_PROBE_{kind.upper()}_MODEL"]
        missing = [key for key in names if not os.getenv(key)]
        if missing:
            print(json.dumps({"kind": kind, "status": "UNVERIFIED", "missing": missing}))
            if exit_code != 1:
                exit_code = 2
            continue
        try:
            value = ModelConfigWrite.model_validate(
                {
                    "providerProtocol": os.getenv(
                        f"EAH_PROBE_{kind.upper()}_PROTOCOL", PROTOCOLS[kind][0]
                    ),
                    "baseUrl": os.environ[names[1]],
                    "modelName": os.environ[names[2]],
                    "credential": os.environ[names[0]],
                    "expectedRevision": 0,
                }
            )

            async def record(_attempt: Attempt) -> None:
                # CLI has no organization; API persistence is covered by separate DB acceptance.
                return None

            outcome = await probe.test(kind, value, record)
            print(
                json.dumps(
                    {
                        "kind": kind,
                        "status": "PASS" if outcome.result.ok else "FAIL",
                        "result": outcome.result.model_dump(mode="json", by_alias=True),
                        "dimension": outcome.dimension,
                        "tokenizer": outcome.tokenizer,
                    },
                    ensure_ascii=False,
                )
            )
            if not outcome.result.ok:
                exit_code = 1
        except (AppError, ValidationError):
            print(json.dumps({"kind": kind, "status": "FAIL", "errorCode": "PROBE_CONFIG_INVALID"}))
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_async(main()))
