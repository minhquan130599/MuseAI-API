from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .chat_media import ChatMediaManager, media_references
from .account_pool import AccountPool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..auth import MuseAuth
from ..client import MuseClient

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
WEB_STATE_DIR = Path(os.environ.get("MUSE_WEB_STATE_DIR", ".muse-web"))
MUSE_STATE_DIR = Path(os.environ.get("MUSE_STATE_DIR", ".muse-state"))
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MUSE_WEB_CONCURRENCY", "20")))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(value: str | None, fallback: str = "upload.bin") -> str:
    normalized = (value or fallback).replace("\\", "/")
    name = Path(normalized).name.strip()
    if not name or name in {".", ".."}:
        name = fallback
    return "".join(ch for ch in name if ch.isalnum() or ch in "._- ")[:160] or fallback


class LoginStartRequest(BaseModel):
    email: str
    region: str = "VN"


class LoginConfirmRequest(BaseModel):
    otp: str


class ChatSendRequest(BaseModel):
    message: str
    account_id: str | None = None
    session_id: str | None = None
    timeout: float = 120.0


class AccountOtpRequest(BaseModel):
    otp: str


class AccountEnabledRequest(BaseModel):
    enabled: bool


@dataclass(slots=True)
class JobRecord:
    id: str
    prompt: str
    status: str = "queued"
    message: str = "Đang chờ xử lý"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    session_id: str | None = None
    account_id: str | None = None
    account_label: str | None = None
    batch_id: str | None = None
    images: list[str] = field(default_factory=list)
    min_videos: int = 1
    timeout: float = 600.0
    videos: list[dict[str, Any]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    download_errors: list[str] = field(default_factory=list)
    error: str | None = None

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["download_urls"] = [
            f"/api/generations/{self.id}/files/{name}" for name in self.downloads
        ]
        return data


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.jobs_file = root / "jobs.json"
        self.upload_root = root / "uploads"
        self.output_root = root / "outputs"
        self.jobs: dict[str, JobRecord] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.jobs_file.exists():
            return
        try:
            raw = json.loads(self.jobs_file.read_text(encoding="utf-8"))
            allowed = set(JobRecord.__dataclass_fields__)
            for item in raw:
                data = {key: value for key, value in item.items() if key in allowed}
                job = JobRecord(**data)
                if job.status in {"queued", "connecting", "generating", "downloading"}:
                    job.status = "interrupted"
                    job.message = "Server đã khởi động lại trước khi job hoàn tất"
                    job.finished_at = utc_now()
                self.jobs[job.id] = job
        except Exception:
            self.jobs = {}

    def persist(self) -> None:
        self.jobs_file.write_text(
            json.dumps(
                [asdict(job) for job in self.jobs.values()],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    async def add(self, job: JobRecord) -> None:
        async with self._lock:
            self.jobs[job.id] = job
            self.persist()

    async def update(self, job_id: str, **changes: Any) -> JobRecord:
        async with self._lock:
            job = self.jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
            self.persist()
            return job

    def get(self, job_id: str) -> JobRecord:
        job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Generation job not found")
        return job

    def list(self) -> list[JobRecord]:
        return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)


class WebService:
    def __init__(self) -> None:
        self.jobs = JobStore(WEB_STATE_DIR)
        self.accounts = AccountPool(WEB_STATE_DIR / "accounts", legacy_state=MUSE_STATE_DIR)
        self._legacy_pending_account_id: str | None = None
        self._chat_clients: dict[str, MuseClient] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self.chat_media = ChatMediaManager(WEB_STATE_DIR / "chat-media")
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

    async def auth_status(self) -> dict[str, Any]:
        accounts = self.accounts.list()
        ready = sum(a["enabled"] and a["status"] == "ready" for a in accounts)
        return {
            "authenticated": ready > 0,
            "outcome": "validated" if ready else "no_ready_accounts",
            "total_accounts": len(accounts),
            "ready_accounts": ready,
        }

    async def start_login(self, email: str, region: str) -> None:
        record = await self.accounts.begin_login(email, region)
        self._legacy_pending_account_id = record["id"]

    async def confirm_login(self, otp: str) -> dict[str, Any]:
        if not self._legacy_pending_account_id:
            raise ValueError("Send OTP first")
        record = await self.accounts.confirm_login(self._legacy_pending_account_id, otp)
        self._legacy_pending_account_id = None
        return {"authenticated": True, "outcome": "validated", "account": record}

    async def close_chat_client(self, account_id: str | None = None) -> None:
        ids = [account_id] if account_id else list(self._chat_clients)
        for item_id in ids:
            client = self._chat_clients.pop(item_id, None)
            if client is not None:
                await client.close()

    def chat_lock(self, account_id: str) -> asyncio.Lock:
        return self._chat_locks.setdefault(account_id, asyncio.Lock())

    async def send_chat_message(
        self,
        *,
        message: str,
        session_id: str | None,
        timeout: float,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        ready = [a for a in self.accounts.list() if a["enabled"] and a["status"] == "ready"]
        if account_id is None:
            if len(ready) != 1:
                raise ValueError("Please select a Muse account for this chat")
            account_id = ready[0]["id"]
        account = self.accounts.get(account_id)
        if account.status != "ready" or not account.enabled:
            account = self.accounts.get((await self.accounts.verify(account_id))["id"])
            if account.status != "ready" or not account.enabled:
                raise ValueError("Muse account is not logged in or is disabled")
        if self.accounts.is_busy(account_id):
            raise ValueError("Tài khoản đang dùng để tạo video; hãy chọn tài khoản khác")

        async with self.chat_lock(account_id):
            client = self._chat_clients.get(account_id)
            if client is None:
                state = self.accounts.state_dir(account_id)
                client = MuseClient(MuseAuth(state_dir=state), state_dir=state)
                try:
                    await client.connect()
                except Exception:
                    await client.close()
                    raise
                self._chat_clients[account_id] = client

            try:
                baseline: set[str] = set()
                if session_id is not None:
                    try:
                        before = await client.history(session_id=session_id, limit=80)
                        baseline = {
                            f"{kind}:{ref.identity}"
                            for kind, ref in media_references(before)
                        }
                    except Exception:
                        pass

                result = await client.send_text(
                    prompt=message, session_id=session_id, timeout=timeout
                )
                media_expected = any(
                    word in message.casefold()
                    for word in (
                        "ảnh", "hình", "video", "clip", "vẽ", "tệp",
                        "file", "pdf", "docx", "excel", "pptx", "xuất bản",
                        "generate image", "generate video", "render",
                        "download", "create image", "create video",
                    )
                )
                media_job = None
                if result.session_id:
                    media_job = self.chat_media.start(
                        session_id=result.session_id,
                        baseline=baseline,
                        fetch=lambda sid: self._fetch_chat_history(account_id, sid),
                        download=lambda kind, ref, target: self._download_chat_media(
                            account_id, kind, ref, target
                        ),
                        timeout=300 if media_expected else 20,
                    )
                return {
                    "ok": True,
                    "account_id": account_id,
                    "session_id": result.session_id,
                    "text": result.text,
                    "media_job_id": media_job.id if media_job else None,
                    "media_expected": media_expected,
                    "attachments": [],
                }
            except Exception:
                await self.close_chat_client(account_id)
                raise

    async def _fetch_chat_history(self, account_id: str, session_id: str) -> Any:
        async with self.chat_lock(account_id):
            client = self._chat_clients.get(account_id)
            if client is None:
                raise RuntimeError("Muse chat connection has closed")
            return await client.history(session_id=session_id, limit=80)

    async def _download_chat_media(
        self, account_id: str, kind: str, ref: Any, target: Path
    ) -> Path:
        async with self.chat_lock(account_id):
            client = self._chat_clients.get(account_id)
            if client is None:
                raise RuntimeError("Muse chat connection has closed")
            if kind == "video":
                return await client.download_video(ref, target)
            if kind == "image":
                return await client.download_image(ref, target)
            if kind == "file":
                return await client._fs().download(ref.path, target)
            raise ValueError("Unsupported Muse attachment type")

    async def run_generation(self, job_id: str, image_paths: list[Path]) -> None:
        job = self.jobs.get(job_id)
        try:
            await self._run_generation(job_id, image_paths)
        except asyncio.CancelledError:
            await self.jobs.update(
                job_id, status="cancelled", message="Đã hủy job",
                finished_at=utc_now()
            )
            raise
        finally:
            if job.account_id:
                await self.accounts.release(job.account_id)
            shutil.rmtree(self.jobs.upload_root / job_id, ignore_errors=True)
            self.jobs.tasks.pop(job_id, None)

    async def _run_generation(self, job_id: str, image_paths: list[Path]) -> None:
        job = self.jobs.get(job_id)
        async with self._semaphore:
            await self.jobs.update(
                job_id,
                status="connecting",
                message="Đang kết nối Muse / Hatch",
                started_at=utc_now(),
            )
            state = self.accounts.state_dir(job.account_id)
            auth = MuseAuth(state_dir=state)
            client = MuseClient(auth, state_dir=state)
            try:
                await client.connect()
                await self.jobs.update(
                    job_id,
                    status="generating",
                    message="Muse đang tạo video. Có thể mất vài phút.",
                )
                output_dir = self.jobs.output_root / job_id
                result = await client.generate_video(
                    prompt=job.prompt,
                    images=image_paths,
                    output_dir=output_dir,
                    timeout=job.timeout,
                    min_videos=job.min_videos,
                    download=True,
                )
                downloads = [path.name for path in result.downloaded]
                videos = [
                    {
                        "filename": name,
                        "url": f"/api/generations/{job_id}/files/{name}",
                        "mime_type": "video/mp4",
                    }
                    for name in downloads
                ]
                status = "completed" if downloads else "completed_no_media"
                message = (
                    f"Hoàn tất: {len(downloads)} file đã tải về"
                    if downloads
                    else "Generation hoàn tất nhưng chưa tải được file media"
                )
                await self.jobs.update(
                    job_id,
                    status=status,
                    message=message,
                    session_id=result.session_id,
                    videos=videos,
                    downloads=downloads,
                    download_errors=result.download_errors,
                    finished_at=utc_now(),
                )
            except asyncio.CancelledError:
                await self.jobs.update(
                    job_id,
                    status="cancelled",
                    message="Đã hủy job",
                    finished_at=utc_now(),
                )
                raise
            except Exception as exc:
                await self.jobs.update(
                    job_id,
                    status="failed",
                    message="Generation thất bại",
                    error=str(exc),
                    finished_at=utc_now(),
                )
            finally:
                await client.close()

    async def shutdown(self) -> None:
        await self.chat_media.close()
        for task in list(self.jobs.tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*list(self.jobs.tasks.values()), return_exceptions=True)
        await self.close_chat_client()
        await self.accounts.close()


service = WebService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        yield
    finally:
        await service.shutdown()


app = FastAPI(
    title="MuseAI-API Web",
    version="0.3.0",
    description="Local web UI and REST API for MuseAI-API.",
    lifespan=lifespan,
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "MuseAI-API Web",
        "concurrency": MAX_CONCURRENT_JOBS,
    }


@app.get("/api/accounts")
async def list_accounts(refresh: bool = False) -> dict[str, Any]:
    if refresh:
        await service.accounts.verify_all()
    accounts = service.accounts.list()
    return {
        "accounts": accounts,
        "total": len(accounts),
        "ready": sum(a["enabled"] and a["status"] == "ready" for a in accounts),
        "available": sum(
            a["enabled"] and a["status"] == "ready" and not a["busy"]
            for a in accounts
        ),
    }


@app.post("/api/accounts")
async def add_account(body: LoginStartRequest) -> dict[str, Any]:
    try:
        return await service.accounts.begin_login(body.email, body.region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Không gửi được OTP Muse") from exc


@app.post("/api/accounts/{account_id}/otp")
async def confirm_account_otp(
    account_id: str, body: AccountOtpRequest
) -> dict[str, Any]:
    try:
        return await service.accounts.confirm_login(account_id, body.otp)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Không xác nhận được OTP Muse") from exc


@app.post("/api/accounts/{account_id}/resend")
async def resend_account_otp(account_id: str) -> dict[str, Any]:
    try:
        record = service.accounts.get(account_id)
        if not record.email:
            raise ValueError("Legacy account requires an email to login")
        return await service.accounts.begin_login(record.email)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Không gửi được OTP Muse") from exc


@app.post("/api/accounts/{account_id}/verify")
async def verify_account(account_id: str) -> dict[str, Any]:
    try:
        return await service.accounts.verify(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/accounts/{account_id}")
async def set_account_enabled(
    account_id: str, body: AccountEnabledRequest
) -> dict[str, Any]:
    try:
        return await service.accounts.set_enabled(account_id, body.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/accounts/{account_id}")
async def remove_account(account_id: str) -> dict[str, Any]:
    try:
        if service.accounts.is_busy(account_id):
            raise RuntimeError("Account is running a task")
        await service.close_chat_client(account_id)
        await service.accounts.remove(account_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/auth/status")
async def auth_status() -> dict[str, Any]:
    return await service.auth_status()


@app.post("/api/auth/start")
async def auth_start(body: LoginStartRequest) -> dict[str, Any]:
    try:
        await service.start_login(str(body.email), body.region)
        return {"ok": True, "message": "OTP sent"}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/auth/confirm")
async def auth_confirm(body: LoginConfirmRequest) -> dict[str, Any]:
    if not body.otp.strip():
        raise HTTPException(status_code=400, detail="OTP is required")
    try:
        return await service.confirm_login(body.otp)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/model")
async def model_info(account_id: str | None = None) -> dict[str, Any]:
    available = [a for a in service.accounts.list()
                 if a["enabled"] and a["status"] == "ready"]
    if not account_id and not available:
        await service.accounts.verify_all()
        available = [a for a in service.accounts.list()
                     if a["enabled"] and a["status"] == "ready"]
    if not account_id:
        if not available:
            raise HTTPException(status_code=400, detail="No ready Muse account")
        account_id = available[0]["id"]
    record = service.accounts.get(account_id)
    if not record.enabled or record.status != "ready":
        raise HTTPException(status_code=400, detail="Account is not ready")
    state = service.accounts.state_dir(account_id)
    auth = MuseAuth(state_dir=state)
    client = MuseClient(auth, state_dir=state)
    try:
        await client.connect()
        value = await client.model_get()
        return {"ok": True, "model": value}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@app.post("/api/chat/send")
async def chat_send(body: ChatSendRequest) -> dict[str, Any]:
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")
    if body.timeout < 5 or body.timeout > 900:
        raise HTTPException(
            status_code=400,
            detail="timeout must be between 5 and 900 seconds",
        )

    try:
        return await service.send_chat_message(
            message=message,
            account_id=body.account_id,
            session_id=body.session_id,
            timeout=body.timeout,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/chat/media/{media_id}")
async def chat_media_status(media_id: str) -> dict[str, Any]:
    media = service.chat_media.get(media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="Chat media job not found")
    return media.public()


@app.get("/api/chat/media/{media_id}/files/{filename}")
async def chat_media_file(
    media_id: str, filename: str, download: bool = False
) -> FileResponse:
    path = service.chat_media.file_path(media_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    import mimetypes

    return FileResponse(
        path,
        filename=filename,
        media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        content_disposition_type="attachment" if download else "inline",
    )


@app.get("/api/generations")
async def list_generations() -> dict[str, Any]:
    return {"jobs": [job.public() for job in service.jobs.list()]}


@app.get("/api/generations/{job_id}")
async def get_generation(job_id: str) -> dict[str, Any]:
    return service.jobs.get(job_id).public()


@app.post("/api/generations", status_code=202)
async def create_generation(
    prompt: str = Form(...),
    timeout: float = Form(600.0),
    min_videos: int = Form(1),
    task_count: int = Form(1),
    images: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    prompt = prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    if timeout < 30 or timeout > 3600:
        raise HTTPException(
            status_code=400, detail="Timeout must be between 30 and 3600 seconds"
        )
    if min_videos < 1 or min_videos > 10:
        raise HTTPException(status_code=400, detail="min_videos must be between 1 and 10")
    if task_count < 1 or task_count > 20:
        raise HTTPException(status_code=400, detail="task_count must be between 1 and 20")

    await service.accounts.verify_all()
    try:
        selected = await service.accounts.reserve_random(task_count)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    batch_id = uuid.uuid4().hex[:12]
    jobs: list[JobRecord] = []
    prepared: list[tuple[JobRecord, list[Path]]] = []
    try:
        job_files: list[tuple[Path, list[Path]]] = []
        for account in selected:
            job_id = uuid.uuid4().hex[:12]
            upload_dir = service.jobs.upload_root / job_id
            upload_dir.mkdir(parents=True, exist_ok=True)
            job_files.append((upload_dir, []))

        image_names: list[str] = []
        for index, upload in enumerate(images):
            if not upload.filename:
                continue
            if not (upload.content_type or "").startswith("image/"):
                raise ValueError("Reference files must be images")
            name = safe_filename(upload.filename, f"image-{index + 1}.bin")
            targets = []
            for upload_dir, paths in job_files:
                path = upload_dir / f"{index + 1:02d}-{name}"
                targets.append((path, paths))
            # Stream once to the first account, copy into the remaining job folders.
            first, first_paths = targets[0]
            total = 0
            with first.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > 32 * 1024 * 1024:
                        raise ValueError("Maximum 32 MB per reference image")
                    handle.write(chunk)
            first_paths.append(first)
            for target, paths in targets[1:]:
                shutil.copyfile(first, target)
                paths.append(target)
            image_names.append(name)

        for account, (upload_dir, paths) in zip(selected, job_files):
            job_id = upload_dir.name
            job = JobRecord(
                id=job_id, prompt=prompt, batch_id=batch_id,
                account_id=account.id, account_label=account.label,
                images=image_names.copy(), timeout=timeout, min_videos=min_videos,
            )
            prepared.append((job, paths))

        for job, paths in prepared:
            await service.jobs.add(job)
            jobs.append(job)

        for job, paths in prepared:
            task = asyncio.create_task(
                service.run_generation(job.id, paths),
                name=f"muse-generation-{job.id}",
            )
            service.jobs.tasks[job.id] = task

        return {
            "ok": True, "batch_id": batch_id, "count": len(jobs),
            "jobs": [job.public() for job in jobs],
        }
    except BaseException as exc:
        active_accounts = {
            job.account_id for job in jobs
            if job.id in service.jobs.tasks
        }
        for account in selected:
            if account.id not in active_accounts:
                await service.accounts.release(account.id)
        for upload_dir, _ in job_files:
            if upload_dir.name not in service.jobs.tasks:
                shutil.rmtree(upload_dir, ignore_errors=True)
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    finally:
        for upload in images:
            await upload.close()


@app.post("/api/generations/{job_id}/cancel")
async def cancel_generation(job_id: str) -> dict[str, Any]:
    job = service.jobs.get(job_id)
    task = service.jobs.tasks.get(job_id)
    if task is None or task.done():
        return {
            "ok": False,
            "status": job.status,
            "message": "Job is not running",
        }
    task.cancel()
    return {"ok": True, "status": "cancelling"}


@app.get("/api/generations/{job_id}/files/{filename}")
async def generation_file(job_id: str, filename: str) -> FileResponse:
    job = service.jobs.get(job_id)
    safe = safe_filename(filename)
    if safe not in job.downloads:
        raise HTTPException(status_code=404, detail="Generated file not found")
    path = service.jobs.output_root / job_id / safe
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Generated file no longer exists",
        )
    return FileResponse(path, media_type="video/mp4", filename=safe)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="web")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the MuseAI-API web interface"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        "muse_ai.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
