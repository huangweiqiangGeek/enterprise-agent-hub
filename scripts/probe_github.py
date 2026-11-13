"""Opt-in real MCP handshake + exact repository call. No credential output or REST fallback."""

import json
import os

from dotenv import load_dotenv

from backend.github_schemas import RepositoryAdd
from backend.runtime import run_async
from backend.services.errors import AppError
from backend.services.github_mcp import GitHubMCP


async def main() -> int:
    load_dotenv()
    names = ("EAH_PROBE_GITHUB_TOKEN", "EAH_PROBE_GITHUB_OWNER", "EAH_PROBE_GITHUB_REPO")
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        print(json.dumps({"status": "UNVERIFIED", "missing": missing}))
        return 2
    try:
        client = GitHubMCP()
        tested = await client.test(os.environ[names[0]])
        assert tested.schema_fingerprint
        identifier, owner, repo = await client.repository(
            os.environ[names[0]],
            tested.schema_fingerprint,
            RepositoryAdd(
                owner=os.environ[names[1]], repo=os.environ[names[2]], share_with_organization=True
            ),
        )
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "protocol": tested.protocol,
                    "schemaFingerprint": tested.schema_fingerprint,
                    "repositoryId": identifier,
                    "repository": f"{owner}/{repo}",
                    "scope": (
                        "real MCP initialize + tools/list + one search_repositories "
                        "tools/call; not T3 acceptance"
                    ),
                }
            )
        )
        return 0
    except AppError as error:
        print(json.dumps({"status": "FAIL", "code": error.code}))
        return 1
    except Exception:
        print(json.dumps({"status": "FAIL", "code": "INVALID_PROBE_CONFIGURATION"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(run_async(main()))
