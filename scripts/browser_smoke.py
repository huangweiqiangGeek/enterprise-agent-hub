"""Run Playwright against disposable PostgreSQL + API + production Nuxt on unused ports."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import psycopg
from alembic import command
from dotenv import load_dotenv
from psycopg import sql
from sqlalchemy.engine import make_url

from backend.checkpoints import setup_checkpoints
from backend.config import Settings
from backend.db import migration_config
from backend.runtime import run_async
from backend.services.storage import Storage

ROOT = Path(__file__).resolve().parent.parent


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Service exited before readiness: pid={process.pid}")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Service did not become ready: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--node", required=True, help="Pinned Node executable: pnpm exec node -p process.execPath"
    )
    parser.add_argument(
        "--model-fixture",
        action="store_true",
        help="D03 controlled provider protocol, not real integration",
    )
    parser.add_argument(
        "--storage", action="store_true", help="Use a disposable bucket on configured S3"
    )
    parser.add_argument("--spec", help="Run one Playwright spec in the isolated environment")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    admin = os.getenv("EAH_TEST_ADMIN_URL")
    if not admin:
        print("UNVERIFIED: EAH_TEST_ADMIN_URL is missing")
        return 2
    node = Path(args.node)
    entry = ROOT / "apps/web/.output/server/index.mjs"
    if not node.is_file() or not entry.is_file():
        raise RuntimeError("Pinned Node and pnpm build output must exist")
    url = make_url(admin)
    admin_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    name = f"eah_d02_browser_{uuid4().hex}"
    target = url.set(database=name, drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )
    api_port, web_port = unused_port(), unused_port()
    api_url, web_url = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{web_port}"
    previous = os.environ.get("EAH_DATABASE_URL")
    processes: list[subprocess.Popen[bytes]] = []
    created = False
    storage = Storage(Settings(s3_bucket=f"eah-browser-{uuid4().hex}")) if args.storage else None
    bucket_created = False
    with TemporaryDirectory(prefix="eah_d02_browser_") as folder:
        log_path = Path(folder) / "services.log"
        with log_path.open("wb") as log:
            try:
                if storage:
                    run_async(storage.initialize())
                    bucket_created = True
                with psycopg.connect(admin_url, autocommit=True) as db:
                    db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                    created = True
                os.environ["EAH_DATABASE_URL"] = target
                command.upgrade(migration_config(), "head")
                run_async(setup_checkpoints(Settings(database_url=target)))
                env = {
                    **os.environ,
                    "EAH_DATABASE_URL": target,
                    "EAH_API_PORT": str(api_port),
                    "EAH_BROWSER_ORIGINS": json.dumps([web_url]),
                    "EAH_WEB_URL": web_url,
                    "NUXT_API_BASE": api_url,
                    "NITRO_HOST": "127.0.0.1",
                    "NITRO_PORT": str(web_port),
                    "EAH_BROWSER_MODEL_FIXTURE": "1" if args.model_fixture else "0",
                    "EAH_BROWSER_STORAGE": "1" if args.storage else "0",
                    **({"EAH_S3_BUCKET": storage.settings.s3_bucket} if storage else {}),
                }
                for label, cmd, ready in (
                    (
                        "api",
                        [
                            sys.executable,
                            "-m",
                            "scripts.browser_fixture_api" if args.model_fixture else "backend",
                        ],
                        f"{api_url}/api/v1/health/ready",
                    ),
                    ("web", [str(node), str(entry)], f"{web_url}/login"),
                ):
                    process = subprocess.Popen(
                        cmd,
                        cwd=ROOT,
                        env=env,
                        stdout=log,
                        stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    processes.append(process)
                    print(
                        json.dumps(
                            {
                                "service": label,
                                "pid": process.pid,
                                "cwd": str(ROOT),
                                "command": cmd,
                                "url": ready,
                            }
                        ),
                        flush=True,
                    )
                    wait_ready(ready, process)
                result = subprocess.run(
                    [
                        str(node),
                        str(ROOT / "apps/web/node_modules/@playwright/test/cli.js"),
                        "test",
                        *([args.spec] if args.spec else []),
                    ],
                    cwd=ROOT / "apps/web",
                    env=env,
                    check=False,
                )
                if result.returncode:
                    log.flush()
                    print(log_path.read_text(encoding="utf-8", errors="replace")[-6000:])
                return result.returncode
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        if sys.platform == "win32":
                            subprocess.run(
                                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL,
                                check=True,
                            )
                        else:
                            process.terminate()
                        process.wait(timeout=10)
                if previous is None:
                    os.environ.pop("EAH_DATABASE_URL", None)
                else:
                    os.environ["EAH_DATABASE_URL"] = previous
                if created:
                    with psycopg.connect(admin_url, autocommit=True) as db:
                        db.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
                if storage and bucket_created:
                    bucket = storage.settings.s3_bucket
                    objects = storage.client.list_objects_v2(Bucket=bucket).get("Contents", [])
                    for obj in objects:
                        storage.client.delete_object(Bucket=bucket, Key=obj["Key"])
                    storage.client.delete_bucket(Bucket=bucket)
                for port in (api_port, web_port):
                    with socket.socket() as probe:
                        assert probe.connect_ex(("127.0.0.1", port)) != 0, (
                            f"Port {port} still active"
                        )
                log.flush()
                if sys.exc_info()[0] is not None:
                    print(log_path.read_text(encoding="utf-8", errors="replace")[-6000:])
                print(
                    json.dumps(
                        {
                            "temporaryDatabaseRemoved": created,
                            "temporaryBucketRemoved": bucket_created,
                            "portsReleased": [api_port, web_port],
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    raise SystemExit(main())
