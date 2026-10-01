from __future__ import annotations

import argparse
import asyncio
import mimetypes
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

from .auth import MuseAuth
from .client import GenerationResult, ImageGenerationResult, MuseClient, TextResult

DEFAULT_STATE_DIR = Path(os.environ.get("MUSE_STATE_DIR", ".muse-state"))
DEFAULT_MCP_DIR = Path(os.environ.get("MUSE_MCP_DIR", ".muse-mcp"))
DEFAULT_OUTPUT_DIR = Path(
    os.environ.get("MUSE_MCP_OUTPUT_DIR", str(DEFAULT_MCP_DIR / "outputs"))
)
DEFAULT_UPLOAD_DIR = Path(
    os.environ.get("MUSE_MCP_UPLOAD_DIR", str(DEFAULT_MCP_DIR / "uploads"))
)
MAX_IMPORTED_FILE_BYTES = int(
    os.environ.get("MUSE_MCP_MAX_FILE_BYTES", str(32 * 1024 * 1024))
)


class OpenAIFileParam(BaseModel):
    """ChatGPT file object passed through openai/fileParams."""

    model_config = ConfigDict(extra="forbid")

    download_url: str
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialize_text_result(
    result: TextResult,
    *,
    include_raw: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "session_id": result.session_id,
        "text": result.text,
    }
    if include_raw:
        payload["stream_events"] = result.stream_events
        payload["history"] = result.history
    return payload


def serialize_generation_result(result: GenerationResult) -> dict[str, Any]:
    # Do not expose Muse-internal paths/URLs to remote MCP clients. Those
    # references are often bound to the Hatch VM/session and are not directly
    # reachable from ChatGPT. _expose_downloaded_files() attaches public tunnel
    # URLs after the media has been downloaded locally.
    return {
        "ok": True,
        "session_id": result.session_id,
        "videos": [
            {
                "mime_type": ref.mime_type or "video/mp4",
                "url": None,
                "download_url": None,
            }
            for ref in result.videos
        ],
        "download_count": len(result.downloaded),
        "download_errors": list(result.download_errors),
    }


def serialize_image_generation_result(
    result: ImageGenerationResult,
) -> dict[str, Any]:
    # Keep only presentation metadata here. Muse-internal media URLs are not
    # useful to remote MCP clients and can point at session-bound VM hosts.
    return {
        "ok": True,
        "session_id": result.session_id,
        "images": [
            {
                "mime_type": ref.mime_type,
                "label": ref.label,
                "width": ref.width,
                "height": ref.height,
                "url": None,
                "download_url": None,
            }
            for ref in result.images
        ],
        "download_count": len(result.downloaded),
        "download_errors": list(result.download_errors),
    }


@dataclass(slots=True)
class VideoJob:
    id: str
    prompt: str
    kind: str = "video"
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    session_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "session_id": self.session_id,
            "result": self.result,
            "error": self.error,
        }


class MuseMCPBridge:
    """Long-lived bridge between MCP tools and Muse/Hatch."""

    def __init__(
        self,
        *,
        state_dir: str | Path = DEFAULT_STATE_DIR,
        output_dir: str | Path = DEFAULT_OUTPUT_DIR,
        public_base_url: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.output_dir = Path(output_dir)
        self.upload_dir = DEFAULT_UPLOAD_DIR
        self.public_base_url = (
            public_base_url.rstrip("/") if public_base_url else None
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._text_client: MuseClient | None = None
        self._text_connect_lock = asyncio.Lock()
        self._text_call_lock = asyncio.Lock()
        self._jobs_lock = asyncio.Lock()
        self.jobs: dict[str, VideoJob] = {}
        self._media_files: dict[str, Path] = {}

    async def _new_client(self) -> MuseClient:
        auth = MuseAuth(state_dir=self.state_dir)
        client = MuseClient(auth, state_dir=self.state_dir)
        await client.connect()
        return client

    async def _get_text_client(self) -> MuseClient:
        if self._text_client is not None:
            return self._text_client

        async with self._text_connect_lock:
            if self._text_client is None:
                self._text_client = await self._new_client()
            return self._text_client

    async def reset_text_client(self) -> None:
        async with self._text_connect_lock:
            if self._text_client is not None:
                try:
                    await self._text_client.close()
                finally:
                    self._text_client = None

    async def auth_status(self) -> dict[str, Any]:
        auth = MuseAuth(state_dir=self.state_dir)
        try:
            if not auth.cookie_path.exists():
                return {
                    "ok": True,
                    "authenticated": False,
                    "state_dir": str(self.state_dir.resolve()),
                    "message": (
                        "No saved Muse session. Run 'muse-ai login --email ...' "
                        "once before using the MCP server."
                    ),
                }
            await auth.load_cookies()
            check = await auth.auth_check()
            return {
                "ok": True,
                "authenticated": check.get("ok") is True,
                "outcome": check.get("outcome"),
                "state_dir": str(self.state_dir.resolve()),
            }
        finally:
            await auth.close()

    async def ping(self) -> dict[str, Any]:
        async with self._text_call_lock:
            client = await self._get_text_client()
            try:
                response = await client.ping()
                return {
                    "ok": True,
                    "status": response.status,
                    "body": response.text(),
                }
            except Exception:
                await self.reset_text_client()
                raise

    async def model(self) -> dict[str, Any]:
        async with self._text_call_lock:
            client = await self._get_text_client()
            try:
                return {
                    "ok": True,
                    "model": await client.model_get(),
                }
            except Exception:
                await self.reset_text_client()
                raise

    async def send_text(
        self,
        *,
        prompt: str,
        session_id: str | None,
        timeout_seconds: float,
        include_raw: bool,
    ) -> dict[str, Any]:
        async with self._text_call_lock:
            client = await self._get_text_client()
            try:
                result = await client.send_text(
                    prompt=prompt,
                    session_id=session_id,
                    timeout=timeout_seconds,
                )
                return serialize_text_result(
                    result,
                    include_raw=include_raw,
                )
            except Exception:
                # Do not automatically replay the prompt after a transport
                # failure because Muse may already have accepted it.
                await self.reset_text_client()
                raise

    async def history(
        self,
        *,
        session_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        async with self._text_call_lock:
            client = await self._get_text_client()
            try:
                return {
                    "ok": True,
                    "session_id": session_id,
                    "history": await client.history(
                        session_id=session_id,
                        limit=limit,
                    ),
                }
            except Exception:
                await self.reset_text_client()
                raise

    @staticmethod
    def _normalize_images(image_paths: list[str] | None) -> list[Path]:
        images: list[Path] = []
        for raw in image_paths or []:
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"reference image not found: {path}")
            images.append(path)
        return images

    @staticmethod
    def _safe_upload_name(
        file: OpenAIFileParam,
        index: int,
    ) -> str:
        raw = (file.file_name or "").replace("\\", "/")
        name = Path(raw).name.strip()
        if not name:
            suffix = mimetypes.guess_extension(file.mime_type or "") or ".bin"
            name = f"{file.file_id or f'upload-{index + 1}'}{suffix}"

        safe = "".join(
            ch for ch in name if ch.isalnum() or ch in "._- ()[]"
        ).strip(" .")
        return safe[:180] or f"upload-{index + 1}.bin"

    async def _download_openai_files(
        self,
        files: list[OpenAIFileParam] | None,
    ) -> tuple[list[Path], Path | None]:
        if not files:
            return [], None

        request_dir = self.upload_dir / uuid.uuid4().hex
        request_dir.mkdir(parents=True, exist_ok=False)
        downloaded: list[Path] = []

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(90.0),
            ) as client:
                for index, file in enumerate(files):
                    parsed = urlparse(file.download_url)
                    if parsed.scheme != "https" or not parsed.netloc:
                        raise ValueError(
                            "ChatGPT file download_url must be an absolute "
                            "HTTPS URL"
                        )

                    filename = self._safe_upload_name(file, index)
                    target = request_dir / f"{index + 1:02d}-{filename}"
                    total = 0

                    async with client.stream(
                        "GET",
                        file.download_url,
                    ) as response:
                        response.raise_for_status()
                        content_type = response.headers.get(
                            "content-type",
                            "",
                        ).lower()

                        if (
                            file.mime_type
                            and content_type
                            and content_type.startswith(("image/", "video/"))
                            and not content_type.startswith(
                                file.mime_type.lower()
                            )
                        ):
                            raise ValueError(
                                "uploaded file content type mismatch: "
                                f"{content_type!r}"
                            )

                        with target.open("wb") as handle:
                            async for chunk in response.aiter_bytes():
                                total += len(chunk)
                                if total > MAX_IMPORTED_FILE_BYTES:
                                    raise ValueError(
                                        "uploaded file exceeds MCP import "
                                        f"limit ({MAX_IMPORTED_FILE_BYTES} bytes)"
                                    )
                                handle.write(chunk)

                    if total == 0:
                        raise ValueError(
                            "uploaded file download returned no data"
                        )
                    downloaded.append(target)

            return downloaded, request_dir
        except Exception:
            shutil.rmtree(request_dir, ignore_errors=True)
            raise

    async def _prepare_reference_images(
        self,
        *,
        image_paths: list[str] | None,
        files: list[OpenAIFileParam] | None,
    ) -> tuple[list[Path], Path | None]:
        local = self._normalize_images(image_paths)
        imported, request_dir = await self._download_openai_files(files)
        return [*local, *imported], request_dir

    def _expose_downloaded_files(
        self,
        payload: dict[str, Any],
        paths: list[Path],
    ) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for raw_path in paths:
            path = Path(raw_path).resolve()
            if not path.is_file():
                continue

            token = uuid.uuid4().hex
            self._media_files[token] = path
            route = f"/media/{token}"
            public_url = (
                f"{self.public_base_url}{route}"
                if self.public_base_url
                else None
            )
            download_url = (
                f"{public_url}?download=1"
                if public_url
                else None
            )
            files.append(
                {
                    "filename": path.name,
                    "mime_hint": (
                        "video/mp4"
                        if path.suffix.lower() == ".mp4"
                        else None
                    ),
                    "public_url": public_url,
                    "download_url": download_url,
                }
            )

        media_items = payload.get("videos")
        if not isinstance(media_items, list):
            media_items = payload.get("images")
        if isinstance(media_items, list):
            for index, item in enumerate(media_items):
                if not isinstance(item, dict) or index >= len(files):
                    continue
                public_file = files[index]
                item["filename"] = public_file["filename"]
                item["url"] = public_file["public_url"]
                item["public_url"] = public_file["public_url"]
                item["download_url"] = public_file["download_url"]

        payload["files"] = files
        payload["public_urls"] = [
            item["public_url"]
            for item in files
            if item["public_url"]
        ]
        payload["download_urls"] = [
            item["download_url"]
            for item in files
            if item["download_url"]
        ]
        if files and not self.public_base_url:
            payload["public_media_warning"] = (
                "Generated files are local only. Start muse-mcp with "
                "--public-base-url https://<your-tunnel-host> so remote MCP "
                "clients receive downloadable HTTPS links."
            )
        return payload

    def media_path(self, token: str) -> Path | None:
        path = self._media_files.get(token)
        if path is None or not path.is_file():
            return None
        return path

    async def generate_video_wait(
        self,
        *,
        prompt: str,
        image_paths: list[str] | None,
        files: list[OpenAIFileParam] | None,
        timeout_seconds: float,
        min_videos: int,
        download: bool,
        output_dir: str | None,
    ) -> dict[str, Any]:
        images, request_dir = await self._prepare_reference_images(
            image_paths=image_paths,
            files=files,
        )
        client = await self._new_client()
        try:
            result = await client.generate_video(
                prompt=prompt,
                images=images,
                output_dir=Path(output_dir).expanduser()
                if output_dir
                else self.output_dir,
                timeout=timeout_seconds,
                min_videos=min_videos,
                download=download,
            )
            return self._expose_downloaded_files(
                serialize_generation_result(result),
                result.downloaded,
            )
        finally:
            await client.close()
            if request_dir is not None:
                shutil.rmtree(request_dir, ignore_errors=True)

    async def submit_video_job(
        self,
        *,
        prompt: str,
        image_paths: list[str] | None,
        files: list[OpenAIFileParam] | None,
        timeout_seconds: float,
        min_videos: int,
        download: bool,
        output_dir: str | None,
    ) -> dict[str, Any]:
        images, request_dir = await self._prepare_reference_images(
            image_paths=image_paths,
            files=files,
        )
        job_id = uuid.uuid4().hex[:12]
        job = VideoJob(id=job_id, prompt=prompt)

        async with self._jobs_lock:
            self.jobs[job_id] = job

        task = asyncio.create_task(
            self._run_video_job(
                job,
                images=images,
                timeout_seconds=timeout_seconds,
                min_videos=min_videos,
                download=download,
                output_dir=output_dir,
                request_dir=request_dir,
            ),
            name=f"muse-mcp-video-{job_id}",
        )
        job.task = task
        return {
            "ok": True,
            "job_id": job_id,
            "status": "queued",
            "message": (
                "Video generation submitted. Call muse_job_status with this "
                "job_id until status is completed or failed."
            ),
        }

    async def _run_video_job(
        self,
        job: VideoJob,
        *,
        images: list[Path],
        timeout_seconds: float,
        min_videos: int,
        download: bool,
        output_dir: str | None,
        request_dir: Path | None,
    ) -> None:
        job.status = "connecting"
        job.started_at = utc_now()
        client: MuseClient | None = None
        try:
            client = await self._new_client()
            job.status = "generating"
            destination = (
                Path(output_dir).expanduser()
                if output_dir
                else self.output_dir / job.id
            )
            result = await client.generate_video(
                prompt=job.prompt,
                images=images,
                output_dir=destination,
                timeout=timeout_seconds,
                min_videos=min_videos,
                download=download,
            )
            job.session_id = result.session_id
            job.result = self._expose_downloaded_files(
                serialize_generation_result(result),
                result.downloaded,
            )
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "job cancelled"
            raise
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = utc_now()
            if client is not None:
                await client.close()
            if request_dir is not None:
                shutil.rmtree(request_dir, ignore_errors=True)

    async def generate_image_wait(
        self,
        *,
        prompt: str,
        image_paths: list[str] | None,
        files: list[OpenAIFileParam] | None,
        timeout_seconds: float,
        min_images: int,
        download: bool,
        output_dir: str | None,
    ) -> dict[str, Any]:
        images, request_dir = await self._prepare_reference_images(
            image_paths=image_paths,
            files=files,
        )
        client = await self._new_client()
        try:
            result = await client.generate_image(
                prompt=prompt,
                images=images,
                output_dir=Path(output_dir).expanduser()
                if output_dir
                else self.output_dir,
                timeout=timeout_seconds,
                min_images=min_images,
                download=download,
            )
            return self._expose_downloaded_files(
                serialize_image_generation_result(result),
                result.downloaded,
            )
        finally:
            await client.close()
            if request_dir is not None:
                shutil.rmtree(request_dir, ignore_errors=True)

    async def submit_image_job(
        self,
        *,
        prompt: str,
        image_paths: list[str] | None,
        files: list[OpenAIFileParam] | None,
        timeout_seconds: float,
        min_images: int,
        download: bool,
        output_dir: str | None,
    ) -> dict[str, Any]:
        images, request_dir = await self._prepare_reference_images(
            image_paths=image_paths,
            files=files,
        )
        job_id = uuid.uuid4().hex[:12]
        job = VideoJob(id=job_id, prompt=prompt, kind="image")

        async with self._jobs_lock:
            self.jobs[job_id] = job

        task = asyncio.create_task(
            self._run_image_job(
                job,
                images=images,
                timeout_seconds=timeout_seconds,
                min_images=min_images,
                download=download,
                output_dir=output_dir,
                request_dir=request_dir,
            ),
            name=f"muse-mcp-image-{job_id}",
        )
        job.task = task
        return {
            "ok": True,
            "job_id": job_id,
            "kind": "image",
            "status": "queued",
            "message": (
                "Image generation submitted. Call muse_job_status with this "
                "job_id until status is completed or failed."
            ),
        }

    async def _run_image_job(
        self,
        job: VideoJob,
        *,
        images: list[Path],
        timeout_seconds: float,
        min_images: int,
        download: bool,
        output_dir: str | None,
        request_dir: Path | None,
    ) -> None:
        job.status = "connecting"
        job.started_at = utc_now()
        client: MuseClient | None = None
        try:
            client = await self._new_client()
            job.status = "generating"
            destination = (
                Path(output_dir).expanduser()
                if output_dir
                else self.output_dir / job.id
            )
            result = await client.generate_image(
                prompt=job.prompt,
                images=images,
                output_dir=destination,
                timeout=timeout_seconds,
                min_images=min_images,
                download=download,
            )
            job.session_id = result.session_id
            job.result = self._expose_downloaded_files(
                serialize_image_generation_result(result),
                result.downloaded,
            )
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "job cancelled"
            raise
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = utc_now()
            if client is not None:
                await client.close()
            if request_dir is not None:
                shutil.rmtree(request_dir, ignore_errors=True)

    async def job_status(self, job_id: str) -> dict[str, Any]:
        async with self._jobs_lock:
            job = self.jobs.get(job_id)
        if job is None:
            return {
                "ok": False,
                "error": "job_not_found",
                "job_id": job_id,
            }
        return {
            "ok": True,
            **job.public(),
        }

    async def list_jobs(self, limit: int) -> dict[str, Any]:
        async with self._jobs_lock:
            jobs = list(self.jobs.values())
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        return {
            "ok": True,
            "jobs": [job.public() for job in jobs[:limit]],
        }

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        async with self._jobs_lock:
            job = self.jobs.get(job_id)
        if job is None:
            return {
                "ok": False,
                "error": "job_not_found",
                "job_id": job_id,
            }
        if job.task is None or job.task.done():
            return {
                "ok": False,
                "job_id": job_id,
                "status": job.status,
                "message": "job is not running",
            }
        job.task.cancel()
        return {
            "ok": True,
            "job_id": job_id,
            "status": "cancelling",
        }

    async def close(self) -> None:
        for job in list(self.jobs.values()):
            if job.task is not None and not job.task.done():
                job.task.cancel()
        await self.reset_text_client()


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    public_base_url: str | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> FastMCP:
    bridge = MuseMCPBridge(
        state_dir=state_dir,
        output_dir=output_dir,
        public_base_url=public_base_url,
    )

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        try:
            yield {"bridge": bridge}
        finally:
            await bridge.close()

    extra_hosts = list(allowed_hosts or [])
    extra_origins = list(allowed_origins or [])
    if public_base_url:
        parsed = urlparse(public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "public_base_url must be an absolute http(s) URL, for example "
                "https://example.trycloudflare.com"
            )
        if parsed.netloc not in extra_hosts:
            extra_hosts.append(parsed.netloc)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in extra_origins:
            extra_origins.append(origin)

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            *extra_hosts,
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            *extra_origins,
        ],
    )

    server = FastMCP(
        "MuseAI",
        instructions=(
            "Use Muse through the user's authenticated local session. "
            "muse_send_text is only for normal text conversation; preserve its "
            "returned session_id for multi-turn text chats. For any request to "
            "create an image, call muse_generate_image. For any request to create "
            "a video, call muse_generate_video. Never present Muse/Hatch internal "
            "metaaivm.com media URLs to the user. Generation results expose "
            "public_url/download_url fields backed by this MCP server; use those "
            "links only. For image or video generation, use wait=true for a "
            "blocking result or wait=false plus muse_job_status for asynchronous "
            "operation."
        ),
        host=host,
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        transport_security=transport_security,
        lifespan=lifespan,
    )

    @server.custom_route(
        "/media/{media_id}",
        methods=["GET"],
        include_in_schema=False,
    )
    async def serve_generated_media(request: Request) -> Response:
        media_id = request.path_params.get("media_id", "")
        path = bridge.media_path(media_id)
        if path is None:
            return PlainTextResponse("media not found", status_code=404)

        download = request.query_params.get("download", "").lower() in {
            "1",
            "true",
            "yes",
        }
        return FileResponse(
            path,
            filename=path.name,
            content_disposition_type="attachment" if download else "inline",
        )

    @server.tool(
        description=(
            "Check whether a reusable Muse session exists and is currently "
            "validated. This does not send a chat message."
        )
    )
    async def muse_auth_status() -> dict[str, Any]:
        return await bridge.auth_status()

    @server.tool(
        description="Verify the Muse/Hatch/Noise connection with a ping."
    )
    async def muse_ping() -> dict[str, Any]:
        return await bridge.ping()

    @server.tool(
        description="Return information about the active Muse model."
    )
    async def muse_model() -> dict[str, Any]:
        return await bridge.model()

    @server.tool(
        description=(
            "Send plain text to Muse and return Muse's assistant text response. "
            "Use this only for text conversation, not for image/video generation. "
            "Pass the returned session_id into the next call to continue the "
            "same conversation."
        )
    )
    async def muse_send_text(
        prompt: str,
        session_id: str | None = None,
        timeout_seconds: float = 120.0,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        if timeout_seconds < 5 or timeout_seconds > 900:
            raise ValueError("timeout_seconds must be between 5 and 900")
        return await bridge.send_text(
            prompt=prompt,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
            include_raw=include_raw,
        )

    @server.tool(
        description=(
            "Read Muse chat history. Provide session_id to inspect one "
            "conversation, or omit it for the default/recent history."
        )
    )
    async def muse_history(
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        return await bridge.history(
            session_id=session_id,
            limit=limit,
        )

    @server.tool(
        description=(
            "Generate image(s) in Muse from a text prompt. Optional ChatGPT-uploaded "
            "reference images are accepted through the images parameter; local "
            "agents may use image_paths. With wait=true it waits for the result "
            "and returns public_url/download_url links when public-base-url is "
            "configured. Only surface those public links to the user; never use "
            "Muse/Hatch internal media URLs. With wait=false it returns a job_id "
            "immediately; use muse_job_status to poll it."
        ),
        meta={"openai/fileParams": ["images"]},
    )
    async def muse_generate_image(
        prompt: str,
        images: list[OpenAIFileParam] | None = None,
        image_paths: list[str] | None = None,
        wait: bool = True,
        timeout_seconds: float = 300.0,
        min_images: int = 1,
        download: bool = True,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if timeout_seconds < 15 or timeout_seconds > 1800:
            raise ValueError("timeout_seconds must be between 15 and 1800")
        if min_images < 1 or min_images > 12:
            raise ValueError("min_images must be between 1 and 12")

        if wait:
            return await bridge.generate_image_wait(
                prompt=prompt,
                image_paths=image_paths,
                files=images,
                timeout_seconds=timeout_seconds,
                min_images=min_images,
                download=download,
                output_dir=output_dir,
            )

        return await bridge.submit_image_job(
            prompt=prompt,
            image_paths=image_paths,
            files=images,
            timeout_seconds=timeout_seconds,
            min_images=min_images,
            download=download,
            output_dir=output_dir,
        )

    @server.tool(
        description=(
            "Generate a Muse video from a text prompt. ChatGPT-uploaded reference "
            "images are accepted through the images parameter; local agents may "
            "use image_paths. Use this tool for all video-generation requests. "
            "With wait=true it waits for the result and returns "
            "public_url/download_url links when public-base-url is configured. "
            "Only surface those public links to the user; never use Muse/Hatch "
            "internal media URLs. With wait=false it returns a job_id "
            "immediately; use muse_job_status to poll it."
        ),
        meta={"openai/fileParams": ["images"]},
    )
    async def muse_generate_video(
        prompt: str,
        images: list[OpenAIFileParam] | None = None,
        image_paths: list[str] | None = None,
        wait: bool = True,
        timeout_seconds: float = 600.0,
        min_videos: int = 1,
        download: bool = True,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if timeout_seconds < 30 or timeout_seconds > 3600:
            raise ValueError("timeout_seconds must be between 30 and 3600")
        if min_videos < 1 or min_videos > 10:
            raise ValueError("min_videos must be between 1 and 10")

        if wait:
            return await bridge.generate_video_wait(
                prompt=prompt,
                image_paths=image_paths,
                files=images,
                timeout_seconds=timeout_seconds,
                min_videos=min_videos,
                download=download,
                output_dir=output_dir,
            )

        return await bridge.submit_video_job(
            prompt=prompt,
            image_paths=image_paths,
            files=images,
            timeout_seconds=timeout_seconds,
            min_videos=min_videos,
            download=download,
            output_dir=output_dir,
        )

    @server.tool(
        description=(
            "Get the status/result of an asynchronous Muse image or video "
            "generation job returned by a generation tool with wait=false."
        )
    )
    async def muse_job_status(job_id: str) -> dict[str, Any]:
        return await bridge.job_status(job_id)

    @server.tool(
        description="List image/video generation jobs created by this MCP process."
    )
    async def muse_list_jobs(limit: int = 20) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        return await bridge.list_jobs(limit)

    @server.tool(
        description="Cancel a running asynchronous Muse video generation job."
    )
    async def muse_cancel_job(job_id: str) -> dict[str, Any]:
        return await bridge.cancel_job(job_id)

    # Exposed for tests and advanced embedding without making it part of MCP.
    server._muse_bridge = bridge  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run MuseAI as an MCP server for other agents."
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=os.environ.get("MUSE_MCP_TRANSPORT", "stdio"),
        help="MCP transport. stdio is recommended for local agent configs.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MUSE_MCP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MUSE_MCP_PORT", "8765")),
    )
    parser.add_argument(
        "--public-base-url",
        default=os.environ.get("MUSE_MCP_PUBLIC_BASE_URL"),
        help=(
            "Public HTTP(S) origin used to build generated media links, e.g. "
            "https://example.trycloudflare.com. Its host is automatically "
            "added to the MCP Host allowlist."
        ),
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[
            value.strip()
            for value in os.environ.get("MUSE_MCP_ALLOWED_HOSTS", "").split(",")
            if value.strip()
        ],
        help=(
            "Additional exact Host header allowed by MCP DNS-rebinding "
            "protection. Repeat for multiple hosts."
        ),
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[
            value.strip()
            for value in os.environ.get("MUSE_MCP_ALLOWED_ORIGINS", "").split(",")
            if value.strip()
        ],
        help=(
            "Additional exact Origin allowed by MCP DNS-rebinding protection. "
            "Repeat for multiple origins."
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("MUSE_STATE_DIR", str(DEFAULT_STATE_DIR)),
        help="Directory containing cookies.json/device.json from muse-ai login.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get(
            "MUSE_MCP_OUTPUT_DIR",
            str(DEFAULT_OUTPUT_DIR),
        ),
    )
    args = parser.parse_args()

    server = build_server(
        host=args.host,
        port=args.port,
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        public_base_url=args.public_base_url,
        allowed_hosts=args.allowed_host,
        allowed_origins=args.allowed_origin,
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
