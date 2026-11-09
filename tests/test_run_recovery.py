"""D10: real PostgreSQL transactions and killed independent worker processes."""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_agents import Setup
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_knowledge import application
from test_knowledge import setup as setup
from test_runs import QAProvider, accept, conversation, execute, snapshot

from backend.checkpoints import setup_checkpoints
from backend.services.errors import AppError
from backend.services.run_state import RunState
from backend.workers.runs import reconcile

pytestmark = pytest.mark.integration


def state_for(setup: Setup, rid: str) -> RunState:
    return RunState(application(setup[0]).state.engine, setup[3], UUID(setup[1].org), UUID(rid))


def sweep(setup: Setup) -> None:
    assert setup[0].portal
    setup[0].portal.call(reconcile, application(setup[0]).state.engine, setup[3])


def expire(url: str, rid: str) -> None:
    with connection(url) as db:
        db.execute(
            "UPDATE app.runs SET lease_until=now()-interval '1 second' "
            "WHERE id=%s AND status='running'",
            (rid,),
        )


def invariants(url: str, rid: str) -> None:
    with connection(url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s AND type IN "
            "('run.completed','run.failed','run.cancelled','run.timed_out')",
            (rid,),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM app.usage_entries WHERE run_id=%s AND status='pending'", (rid,)
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT lease_owner,lease_until FROM app.runs WHERE id=%s", (rid,)
        ).fetchone() == (None, None)
        seq = [
            r[0]
            for r in db.execute(
                "SELECT seq FROM app.run_events WHERE run_id=%s ORDER BY seq", (rid,)
            )
        ]
        assert seq == list(range(1, len(seq) + 1))


def test_cancel_queue_running_race_and_retry(setup: Setup, database_url: str) -> None:
    conv, agent = conversation(setup)
    owner = setup[1]
    rid = accept(setup, conv, agent).json()["data"]["runId"]
    assert owner.request("POST", f"/runs/{rid}/cancel").status_code == 202
    assert owner.request("POST", f"/runs/{rid}/cancel").status_code == 200
    execute(setup, rid, QAProvider())
    assert snapshot(setup, rid)["status"] == "cancelled"
    invariants(database_url, rid)
    # A manual retry snapshots the current saved Agent, not the old failed version.
    update = {
        k: agent[k]
        for k in ("name", "taskType", "knowledgeBaseIds", "toolKeys", "chatConnectionId", "enabled")
    }
    updated = owner.request(
        "PUT",
        f"/agents/{agent['id']}",
        json={**update, "prompt": "Updated retry prompt", "expectedRevision": agent["revision"]},
    )
    assert updated.status_code == 200, updated.text
    body = {"agentId": agent["id"], "input": snapshot(setup, rid)["input"], "retryOfRunId": rid}
    key = uuid4().hex
    retry = owner.request(
        "POST", f"/conversations/{conv}/runs", json=body, headers={"Idempotency-Key": key}
    ).json()["data"]["runId"]
    assert retry != rid
    assert snapshot(setup, retry)["configVersion"]["versionNumber"] == 2
    assert (
        owner.request(
            "POST", f"/conversations/{conv}/runs", json=body, headers={"Idempotency-Key": key}
        ).json()["data"]["runId"]
        == retry
    )
    state = state_for(setup, retry)
    assert setup[0].portal
    setup[0].portal.call(state.claim)
    with ThreadPoolExecutor(4) as pool:
        codes = list(
            pool.map(lambda _: owner.request("POST", f"/runs/{retry}/cancel").status_code, range(4))
        )
    assert set(codes) == {202}
    assert snapshot(setup, retry)["cancelRequested"]
    from backend.run_schemas import QAResult

    result = QAResult(
        answer="no evidence", claims=[], conflicts=[], missing_information=["missing"], coverage={}
    )
    setup[0].portal.call(state.terminal, result)
    assert snapshot(setup, retry)["status"] == "cancelled"
    assert snapshot(setup, retry)["retryOfRunId"] == rid
    invariants(database_url, retry)
    # Success committed first wins; a later cancellation returns the original success.
    succeeded = accept(setup, conv, agent).json()["data"]["runId"]
    execute(setup, succeeded, QAProvider())
    response = owner.request("POST", f"/runs/{succeeded}/cancel")
    assert response.status_code == 200 and response.json()["data"]["status"] == "succeeded"
    invariants(database_url, succeeded)


@pytest.mark.parametrize(
    "phase",
    [
        "claimed",
        "tool_saved",
        "tool_unknown",
        "model_saved",
        "partial",
        "before_terminal",
        "after_terminal",
    ],
)
def test_killed_worker_windows(setup: Setup, database_url: str, tmp_path: Path, phase: str) -> None:
    conv, agent = conversation(setup)
    rid = accept(setup, conv, agent).json()["data"]["runId"]
    assert setup[0].portal
    setup[0].portal.call(setup_checkpoints, setup[3])
    marker = tmp_path / "phase.json"
    log = (tmp_path / "worker.log").open("w", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path.cwd()), "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        [
            sys.executable,
            "tests/run_fault_process.py",
            database_url,
            setup[1].org,
            rid,
            phase,
            str(marker),
        ],
        env=env,
        stdout=log,
        stderr=log,
    )
    try:
        end = time.monotonic() + 35
        while not marker.exists() and process.poll() is None and time.monotonic() < end:
            time.sleep(0.1)
        assert marker.exists(), (tmp_path / "worker.log").read_text(encoding="utf-8")
        assert json.loads(marker.read_text())["phase"] == phase
        before = snapshot(setup, rid)
        process.kill()
        process.wait(timeout=10)
        expire(database_url, rid)
        sweep(setup)
        if phase == "partial":
            final = snapshot(setup, rid)
            assert final["errorCode"] == "RUN_INTERRUPTED" and final["partialMessage"]
        else:
            # Resume in a second OS process; no Python objects survive the crash.
            recovered = subprocess.run(
                [
                    sys.executable,
                    "tests/run_fault_process.py",
                    database_url,
                    setup[1].org,
                    rid,
                    "none",
                    str(marker),
                ],
                env=env,
                stdout=log,
                stderr=log,
                timeout=35,
                check=False,
            )
            assert recovered.returncode == 0
            final = snapshot(setup, rid)
            assert final["status"] == "succeeded", final
        assert final["startedAt"] == before["startedAt"]
        assert final["deadlineAt"] == before["deadlineAt"]
        assert final["budgetSnapshot"] == before["budgetSnapshot"]
        with connection(database_url) as db:
            rows = db.execute(
                "SELECT operation_kind,status,reserved_tokens FROM app.usage_entries "
                "WHERE run_id=%s",
                (rid,),
            ).fetchall()
            if phase == "tool_unknown":
                assert any(
                    r[0] == "run.embedding" and r[1] == "outcome_unknown" and r[2] > 0 for r in rows
                )
                assert sum(r[0] == "run.embedding" for r in rows) == 2
            if phase in {"tool_saved", "model_saved", "before_terminal", "after_terminal"}:
                assert sum(r[0] == "run.embedding" for r in rows) == 1
                assert sum(r[0] == "run.chat" for r in rows) == 1
        invariants(database_url, rid)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        log.close()


def test_recovery_bounds_fences_deadline_and_admin(setup: Setup, database_url: str) -> None:
    conv, agent = conversation(setup)
    rid = accept(setup, conv, agent).json()["data"]["runId"]
    old = state_for(setup, rid)
    assert setup[0].portal
    setup[0].portal.call(old.claim)
    for generation in (2, 3):
        expire(database_url, rid)
        sweep(setup)
        with connection(database_url) as db:
            assert db.execute(
                "SELECT lease_generation FROM app.runs WHERE id=%s", (rid,)
            ).fetchone() == (generation,)
        with pytest.raises(AppError):
            setup[0].portal.call(old.delta, "stale")
        assert not setup[0].portal.call(old.terminal, None, "MODEL_UNAVAILABLE")
        fresh = state_for(setup, rid)
        assert setup[0].portal.call(fresh.claim)
    expire(database_url, rid)
    sweep(setup)
    assert snapshot(setup, rid)["errorCode"] == "RUN_INTERRUPTED"
    invariants(database_url, rid)
    timed = accept(setup, conv, agent).json()["data"]["runId"]
    current = state_for(setup, timed)
    setup[0].portal.call(current.claim)
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.runs SET deadline_at=now()-interval '1 second', "
            "lease_until=now()+interval '1 hour' WHERE id=%s",
            (timed,),
        )
    sweep(setup)
    assert snapshot(setup, timed)["status"] == "timed_out"
    invariants(database_url, timed)
    # Admin may audit a member's run, never cancel on their behalf.
    member = Actor(setup[0]).register()
    response = setup[1].request(
        "POST", "/members", json={"email": member.email}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert response.status_code == 201
    member.org = setup[1].org
    cid = member.request(
        "POST",
        "/conversations",
        json={"agentId": agent["id"], "taskType": "qa"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["id"]
    other = member.request(
        "POST",
        f"/conversations/{cid}/runs",
        json={
            "agentId": agent["id"],
            "input": {"question": "review?", "knowledgeBaseIds": [setup[2]]},
        },
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["runId"]
    assert setup[1].request("POST", f"/runs/{other}/cancel").status_code == 404
    assert member.request("POST", f"/runs/{other}/cancel").status_code == 202


@pytest.mark.parametrize("action", ["cancel", "deadline", "stale"])
def test_live_worker_stop_and_stale_takeover(
    setup: Setup, database_url: str, tmp_path: Path, action: str
) -> None:
    conv, agent = conversation(setup)
    rid = accept(setup, conv, agent).json()["data"]["runId"]
    assert setup[0].portal
    setup[0].portal.call(setup_checkpoints, setup[3])
    marker = tmp_path / "live.json"
    phase = "tool_saved" if action == "stale" else "partial"
    env = {**os.environ, "PYTHONPATH": str(Path.cwd()), "PYTHONIOENCODING": "utf-8"}
    with (tmp_path / "live.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "tests/run_fault_process.py",
                database_url,
                setup[1].org,
                rid,
                phase,
                str(marker),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            end = time.monotonic() + 35
            while not marker.exists() and process.poll() is None and time.monotonic() < end:
                time.sleep(0.1)
            assert marker.exists(), (tmp_path / "live.log").read_text(encoding="utf-8")
            if action == "stale":
                expire(database_url, rid)
                sweep(setup)
                execute(setup, rid, QAProvider())
                final_before = snapshot(setup, rid)
                marker.with_suffix(".release").write_text("release", encoding="utf-8")
            elif action == "cancel":
                for _ in range(3):
                    assert setup[1].request("POST", f"/runs/{rid}/cancel").status_code in {200, 202}
            else:
                with connection(database_url) as db:
                    db.execute(
                        "UPDATE app.runs SET deadline_at=now()-interval '1 second' WHERE id=%s",
                        (rid,),
                    )
                sweep(setup)
            # Healthy heartbeat must cancel the open graph/stream and let this process exit.
            assert process.wait(timeout=12) == 0
            final = snapshot(setup, rid)
            assert (
                final["status"]
                == {"cancel": "cancelled", "deadline": "timed_out", "stale": "succeeded"}[action]
            )
            if action == "stale":
                assert final == final_before
            else:
                assert final["partialMessage"] and final["result"] is None
                with connection(database_url) as db:
                    row = db.execute(
                        "SELECT status,token_source,reserved_tokens FROM app.usage_entries "
                        "WHERE run_id=%s AND operation_kind='run.chat'",
                        (rid,),
                    ).fetchone()
                    assert (
                        row and row[0] == "outcome_unknown" and row[1] == "reserved" and row[2] > 0
                    )
            invariants(database_url, rid)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_recovery_preserves_tool_budget_and_graph_compatibility(
    setup: Setup, database_url: str
) -> None:
    conv, agent = conversation(setup)
    rid = accept(setup, conv, agent).json()["data"]["runId"]
    state = state_for(setup, rid)
    assert setup[0].portal
    setup[0].portal.call(state.claim)
    setup[0].portal.call(state.reserve, "search:" + setup[2], "tool", "knowledge.search", {})
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.runs SET budget_snapshot="
            "jsonb_set(budget_snapshot,'{max_tool_attempts}','1') WHERE id=%s",
            (rid,),
        )
    expire(database_url, rid)
    sweep(setup)
    execute(setup, rid, QAProvider())
    assert snapshot(setup, rid)["errorCode"] == "BUDGET_EXCEEDED"
    invariants(database_url, rid)
    incompatible = accept(setup, conv, agent).json()["data"]["runId"]
    with connection(database_url) as db:
        db.execute("UPDATE app.runs SET graph_version='unknown-graph' WHERE id=%s", (incompatible,))
    execute(setup, incompatible, QAProvider())
    assert snapshot(setup, incompatible)["errorCode"] == "RUN_INTERRUPTED"


def test_public_cancel_vs_success_concurrent_transactions(setup: Setup, database_url: str) -> None:
    from backend.run_schemas import QAResult

    conv, agent = conversation(setup)
    assert setup[0].portal
    for _ in range(6):
        rid = accept(setup, conv, agent).json()["data"]["runId"]
        state = state_for(setup, rid)
        setup[0].portal.call(state.claim)
        barrier = threading.Barrier(2)

        def cancel(gate: threading.Barrier, target: str) -> int:
            gate.wait(timeout=5)
            return setup[1].request("POST", f"/runs/{target}/cancel").status_code

        def publish(gate: threading.Barrier, current: RunState) -> bool:
            gate.wait(timeout=5)
            assert setup[0].portal
            return setup[0].portal.call(
                current.terminal,
                QAResult(
                    answer="资料不足",
                    claims=[],
                    conflicts=[],
                    missing_information=["资料不足"],
                    coverage={},
                ),
            )

        with ThreadPoolExecutor(2) as pool:
            cancellation, completion = (
                pool.submit(cancel, barrier, rid),
                pool.submit(publish, barrier, state),
            )
            code, committed = cancellation.result(), completion.result()
        assert committed
        result = snapshot(setup, rid)
        assert result["status"] == ("cancelled" if code == 202 else "succeeded")
        assert result["cancelRequested"] == (code == 202)
        invariants(database_url, rid)
