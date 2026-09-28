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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..auth import MuseAuth
from ..client import MuseClient

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
WEB_STATE_DIR = Path(os.environ.get("MUSE_WEB_STATE_DIR", ".muse-web"))
MUSE_STATE_DIR = Path(os.environ.get("MUSE_STATE_DIR", ".muse-state"))
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MUSE_WEB_CONCURRENCY", "2")))


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
        self._auth: MuseAuth | None = None
        self._auth_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

    async def auth(self) -> MuseAuth:
        if self._auth is None:
            self._auth = MuseAuth(state_dir=MUSE_STATE_DIR)
            if self._auth.cookie_path.exists():
                try:
                    await self._auth.load_cookies()
                except Exception:
                    pass
        return self._auth

    async def auth_status(self) -> dict[str, Any]:
        auth = await self.auth()
        if not auth.cookie_path.exists():
            return {"authenticated": False, "outcome": "no_session"}
        try:
            check = await auth.auth_check()
        except Exception as exc:
            return {"authenticated": False, "outcome": "error", "detail": str(exc)}
        return {
            "authenticated": check.get("ok") is True,
            "outcome": check.get("outcome")
            or ("validated" if check.get("ok") else "invalid"),
        }

    async def start_login(self, email: str, region: str) -> None:
        async with self._auth_lock:
            if self._auth is not None:
                await self._auth.close()
            self._auth = MuseAuth(state_dir=MUSE_STATE_DIR)
            await self._auth.restart_login()
            await self._auth.send_otp(email, region)

    async def confirm_login(self, otp: str) -> dict[str, Any]:
        async with self._auth_lock:
            auth = await self.auth()
            await auth.confirm_otp(otp.strip())
            await auth.save_account()
            check = await auth.auth_check()
            if check.get("ok") is not True:
                raise HTTPException(status_code=401, detail="Muse session validation failed")
            await auth.save_cookies()
            return {
                "authenticated": True,
                "outcome": check.get("outcome", "validated"),
            }

    async def run_generation(self, job_id: str, image_paths: list[Path]) -> None:
        job = self.jobs.get(job_id)
        async with self._semaphore:
            await self.jobs.update(
                job_id,
                status="connecting",
                message="Đang kết nối Muse / Hatch",
                started_at=utc_now(),
            )
            auth = MuseAuth(state_dir=MUSE_STATE_DIR)
            client = MuseClient(auth, state_dir=MUSE_STATE_DIR)
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
                videos = [
                    {
                        "path": ref.path,
                        "url": ref.url,
                        "mime_type": ref.mime_type,
                        "resource_id": ref.resource_id,
                        "media_handle": ref.media_handle,
                    }
                    for ref in result.videos
                ]
                downloads = [path.name for path in result.downloaded]
                status = "completed" if downloads or videos else "completed_no_media"
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
                upload_dir = self.jobs.upload_root / job_id
                shutil.rmtree(upload_dir, ignore_errors=True)
                self.jobs.tasks.pop(job_id, None)

    async def shutdown(self) -> None:
        for task in list(self.jobs.tasks.values()):
            if not task.done():
                task.cancel()
        if self._auth is not None:
            await self._auth.close()
            self._auth = None


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
async def model_info() -> dict[str, Any]:
    auth = MuseAuth(state_dir=MUSE_STATE_DIR)
    client = MuseClient(auth, state_dir=MUSE_STATE_DIR)
    try:
        await client.connect()
        value = await client.model_get()
        return {"ok": True, "model": value}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


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
    images: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    prompt = prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    if timeout < 30 or timeout > 3600:
        raise HTTPException(
            status_code=400,
            detail="timeout must be between 30 and 3600 seconds",
        )
    if min_videos < 1 or min_videos > 10:
        raise HTTPException(
            status_code=400,
            detail="min_videos must be between 1 and 10",
        )

    auth = await service.auth_status()
    if not auth["authenticated"]:
        raise HTTPException(
            status_code=401,
            detail="Login to Muse before creating a video",
        )

    job_id = uuid.uuid4().hex[:12]
    upload_dir = service.jobs.upload_root / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_images: list[Path] = []
    image_names: list[str] = []

    try:
        for index, upload in enumerate(images):
            if not upload.filename:
                continue
            name = safe_filename(upload.filename, f"image-{index + 1}.bin")
            target = upload_dir / f"{index + 1:02d}-{name}"
            with target.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    handle.write(chunk)
            saved_images.append(target)
            image_names.append(name)
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    finally:
        for upload in images:
            await upload.close()

    job = JobRecord(
        id=job_id,
        prompt=prompt,
        images=image_names,
        timeout=timeout,
        min_videos=min_videos,
    )
    await service.jobs.add(job)
    task = asyncio.create_task(
        service.run_generation(job_id, saved_images),
        name=f"muse-generation-{job_id}",
    )
    service.jobs.tasks[job_id] = task
    return job.public()


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
