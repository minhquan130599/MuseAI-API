from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from muse_ai.web.account_pool import AccountPool, AccountRecord
from muse_ai.web.app import JobStore, app, service


def ready_pool(tmp_path, count: int) -> AccountPool:
    pool = AccountPool(tmp_path / "accounts")
    for index in range(count):
        record = AccountRecord(
            id=f"account-{index+1}",
            email=f"muse{index+1}@example.com",
            label=f"Muse {index+1}",
            status="ready",
        )
        pool._accounts[record.id] = record
    pool._persist()
    return pool


@pytest.mark.asyncio
async def test_pool_random_selection_uses_distinct_ready_accounts(tmp_path):
    pool = ready_pool(tmp_path, 4)
    chosen = await pool.reserve_random(3)
    assert len({account.id for account in chosen}) == 3
    assert all(pool.is_busy(account.id) for account in chosen)
    assert sum(account["busy"] for account in pool.list()) == 3

    with pytest.raises(ValueError, match="tài khoản"):
        await pool.reserve_random(2)

    await pool.release(chosen[0].id)
    new = await pool.reserve_random(2)
    assert len({account.id for account in new}) == 2
    assert chosen[0].id in {account.id for account in new}


@pytest.mark.asyncio
async def test_disable_remove_busy_account_are_rejected(tmp_path):
    pool = ready_pool(tmp_path, 1)
    chosen = await pool.reserve_random(1)
    with pytest.raises(RuntimeError, match="running"):
        await pool.set_enabled(chosen[0].id, False)
    with pytest.raises(RuntimeError, match="running"):
        await pool.remove(chosen[0].id)
    await pool.release(chosen[0].id)
    await pool.set_enabled(chosen[0].id, False)
    with pytest.raises(ValueError, match="tài khoản"):
        await pool.reserve_random(1)
    await pool.remove(chosen[0].id)
    assert pool.list() == []


@pytest.mark.asyncio
async def test_multiple_pending_otps_are_isolated(tmp_path, monkeypatch):
    class FakeAuth:
        def __init__(self, state_dir):
            self.state_dir = Path(state_dir)
        async def restart_login(self):
            return {}
        async def send_otp(self, email, region):
            return {}
        async def confirm_otp(self, otp):
            assert otp in {"123456", "654321"}
        async def save_account(self):
            return {}
        async def auth_check(self):
            return {"ok": True}
        async def save_cookies(self):
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "cookies.json").write_text("{}", encoding="utf-8")
        async def close(self):
            return None

    monkeypatch.setattr("muse_ai.web.account_pool.MuseAuth", FakeAuth)
    pool = AccountPool(tmp_path / "accounts")
    a, b = await asyncio.gather(
        pool.begin_login("first@example.com"),
        pool.begin_login("second@example.com"),
    )
    assert a["id"] != b["id"]
    assert all(item["status"] == "pending_otp" for item in pool.list())
    await asyncio.gather(
        pool.confirm_login(a["id"], "123456"),
        pool.confirm_login(b["id"], "654321"),
    )
    assert all(item["status"] == "ready" for item in pool.list())
    assert pool.state_dir(a["id"]) != pool.state_dir(b["id"])


def test_batch_endpoint_assigns_distinct_accounts(tmp_path, monkeypatch):
    pool = ready_pool(tmp_path, 3)
    job_store = JobStore(tmp_path / "jobs")

    async def fake_verify_all():
        return None

    async def fake_run_generation(job_id, images):
        job = job_store.get(job_id)
        await job_store.update(job_id, status="completed", downloads=[])
        await pool.release(job.account_id)

    monkeypatch.setattr(pool, "verify_all", fake_verify_all)
    monkeypatch.setattr(service, "accounts", pool)
    monkeypatch.setattr(service, "jobs", job_store)
    monkeypatch.setattr(service, "run_generation", fake_run_generation)

    with TestClient(app) as client:
        response = client.post(
            "/api/generations",
            data={
                "prompt": "Create a 9:16 video",
                "task_count": "3",
                "timeout": "300",
                "min_videos": "1",
            },
        )
        assert response.status_code == 202, response.text
        data = response.json()
        assert data["count"] == 3
        assert len({job["account_id"] for job in data["jobs"]}) == 3
        assert len({job["id"] for job in data["jobs"]}) == 3
        assert len({job["batch_id"] for job in data["jobs"]}) == 1
        assert all(job["account_label"] for job in data["jobs"])

        too_many = client.post(
            "/api/generations",
            data={"prompt": "Another video", "task_count": "4"},
        )
        assert too_many.status_code == 409


def test_list_accounts_never_exposes_cookie_contents(tmp_path, monkeypatch):
    pool = ready_pool(tmp_path, 2)
    monkeypatch.setattr(service, "accounts", pool)
    with TestClient(app) as client:
        response = client.get("/api/accounts")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] == 2
    assert payload["available"] == 2
    assert "cookie" not in str(payload).lower()



@pytest.mark.asyncio
async def test_chat_is_bound_to_selected_account(tmp_path, monkeypatch):
    from muse_ai.client import TextResult

    pool = ready_pool(tmp_path, 2)
    seen: list[tuple[str, str | None]] = []

    class FakeChatClient:
        def __init__(self, auth, *, state_dir):
            self.state_dir = Path(state_dir)

        async def connect(self):
            pass

        async def history(self, *, session_id=None, limit=80):
            return {"messages": []}

        async def send_text(self, *, prompt, session_id, timeout):
            account_id = self.state_dir.name
            seen.append((account_id, session_id))
            return TextResult(
                session_id=session_id or f"session-{account_id}",
                text=f"Response from {account_id}",
            )

        async def close(self):
            pass

    class DummyAuth:
        def __init__(self, state_dir):
            self.state_dir = Path(state_dir)

    class DummyMedia:
        id = "job-1"

    monkeypatch.setattr("muse_ai.web.app.MuseClient", FakeChatClient)
    monkeypatch.setattr("muse_ai.web.app.MuseAuth", DummyAuth)
    monkeypatch.setattr(service, "accounts", pool)
    monkeypatch.setattr(service.chat_media, "start", lambda **kwargs: DummyMedia())
    service._chat_clients.clear()

    a = await service.send_chat_message(
        account_id="account-1",
        message="Hello",
        session_id=None,
        timeout=30,
    )
    b = await service.send_chat_message(
        account_id="account-2",
        message="Hello",
        session_id=None,
        timeout=30,
    )
    await service.send_chat_message(
        account_id="account-1",
        message="Continue",
        session_id=a["session_id"],
        timeout=30,
    )

    assert a["account_id"] == "account-1"
    assert b["account_id"] == "account-2"
    assert a["session_id"] != b["session_id"]
    assert seen == [
        ("account-1", None),
        ("account-2", None),
        ("account-1", a["session_id"]),
    ]
    await service.close_chat_client()
