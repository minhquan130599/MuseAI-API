from __future__ import annotations

import asyncio

import httpx
import pytest

from muse_ai.client import GenerationResult, ImageGenerationResult, TextResult
from muse_ai.media import ImageRef, VideoRef
from muse_ai.mcp_server import (
    MuseMCPBridge,
    OpenAIFileParam,
    VideoJob,
    build_server,
    serialize_generation_result,
    serialize_image_generation_result,
    serialize_text_result,
    scrub_internal_media_urls,
)
from muse_ai.text import (
    coalesce_text_fragments,
    extract_assistant_texts,
    extract_text_fragments,
)


def test_extract_assistant_texts_from_role_content():
    payload = {
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hello from Muse"}
                ],
            },
        ]
    }
    assert extract_assistant_texts(payload) == ["Hello from Muse"]


def test_extract_text_fragments_from_json_string():
    payload = {
        "data": '{"role":"assistant","content":"Muse answer"}'
    }
    assert extract_assistant_texts(payload) == ["Muse answer"]
    assert "Muse answer" in extract_text_fragments(payload)


def test_coalesce_cumulative_stream_snapshots():
    assert coalesce_text_fragments(["H", "He", "Hello"]) == "Hello"


def test_coalesce_incremental_stream_chunks():
    assert coalesce_text_fragments(["Hello", " ", "Muse"]) == "HelloMuse"


def test_serialize_text_result_hides_raw_by_default():
    result = TextResult(
        session_id="session-1",
        text="answer",
        stream_events=[{"secret-ish": "raw"}],
        history={"messages": []},
    )
    payload = serialize_text_result(result)
    assert payload == {
        "ok": True,
        "session_id": "session-1",
        "text": "answer",
    }


def test_scrub_internal_media_urls_recursively():
    payload = {
        "text": (
            "watch https://abc.metaaivm.com/media/raw/workspace/video.mp4 "
            "now"
        ),
        "nested": [
            {
                "url": (
                    "https://vm.metaaivm.com/media/raw/workspace/image.webp"
                )
            }
        ],
    }
    cleaned = scrub_internal_media_urls(payload)
    assert "metaaivm.com" not in repr(cleaned)
    assert "public_url/download_url" in cleaned["text"]


def test_serialize_image_generation_result_hides_internal_media(tmp_path):
    image_path = tmp_path / "generated.png"
    result = ImageGenerationResult(
        session_id="session-image",
        images=[
            ImageRef(
                path="sandbox://workspace/user/files/generated.png",
                url="https://vm.metaaivm.com/media/raw/generated.png",
                mime_type="image/png",
                media_handle="image-handle",
                width=1024,
                height=1024,
            )
        ],
        downloaded=[image_path],
    )
    payload = serialize_image_generation_result(result)
    assert payload["session_id"] == "session-image"
    assert payload["images"][0]["width"] == 1024
    assert payload["images"][0]["url"] is None
    assert payload["images"][0]["download_url"] is None
    assert payload["download_count"] == 1
    assert "metaaivm.com" not in repr(payload)
    assert "image-handle" not in repr(payload)


def test_serialize_video_generation_hides_internal_media(tmp_path):
    video_path = tmp_path / "generated.mp4"
    result = GenerationResult(
        session_id="session-video",
        videos=[
            VideoRef(
                path="sandbox://workspace/imagine_media/generated.mp4",
                url="https://vm.metaaivm.com/media/raw/generated.mp4",
                mime_type="video/mp4",
                media_handle="video-handle",
            )
        ],
        downloaded=[video_path],
    )
    payload = serialize_generation_result(result)
    assert payload["session_id"] == "session-video"
    assert payload["videos"][0]["url"] is None
    assert payload["videos"][0]["download_url"] is None
    assert payload["download_count"] == 1
    assert "metaaivm.com" not in repr(payload)
    assert "video-handle" not in repr(payload)


def test_video_job_public_does_not_serialize_asyncio_task():
    async def sleeper():
        await asyncio.sleep(0)

    async def scenario():
        task = asyncio.create_task(sleeper())
        job = VideoJob(id="job-1", prompt="create video", task=task)
        data = job.public()
        assert "task" not in data
        assert data["kind"] == "video"
        await task

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_mcp_server_registers_expected_tools(tmp_path):
    server = build_server(
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "output",
    )
    tools = await server.list_tools()
    by_name = {tool.name: tool for tool in tools}
    assert {
        "muse_auth_status",
        "muse_ping",
        "muse_model",
        "muse_send_text",
        "muse_history",
        "muse_generate_image",
        "muse_generate_video",
        "muse_generate_video_from_images",
        "muse_job_status",
        "muse_list_jobs",
        "muse_cancel_job",
    } <= set(by_name)

    for tool_name in (
        "muse_generate_image",
        "muse_generate_video",
        "muse_generate_video_from_images",
    ):
        tool = by_name[tool_name]
        assert tool.meta == {"openai/fileParams": ["images"]}
        file_schema = tool.inputSchema["$defs"]["OpenAIFileParam"]
        assert set(file_schema["properties"]) == {
            "download_url",
            "file_id",
            "mime_type",
            "file_name",
        }
        assert file_schema["required"] == ["download_url", "file_id"]
        assert file_schema["additionalProperties"] is False

    upload_video_schema = by_name[
        "muse_generate_video_from_images"
    ].inputSchema
    assert set(upload_video_schema["required"]) == {"prompt", "images"}

    bridge = server._muse_bridge
    await bridge.close()


@pytest.mark.asyncio
async def test_public_base_url_forces_video_download(tmp_path, monkeypatch):
    bridge = MuseMCPBridge(
        output_dir=tmp_path / "output",
        public_base_url="https://example.trycloudflare.com",
    )
    seen: dict[str, bool] = {}

    class FakeClient:
        async def generate_video(self, **kwargs):
            seen["download"] = kwargs["download"]
            return GenerationResult(
                session_id="session-force-download",
                videos=[],
                downloaded=[],
            )

        async def close(self):
            return None

    async def fake_new_client():
        return FakeClient()

    monkeypatch.setattr(bridge, "_new_client", fake_new_client)

    await bridge.generate_video_wait(
        prompt="create video",
        image_paths=None,
        files=None,
        timeout_seconds=30,
        min_videos=1,
        download=False,
        output_dir=None,
    )
    assert seen["download"] is True


@pytest.mark.asyncio
async def test_chatgpt_file_param_downloads_to_temp_image(
    tmp_path,
    monkeypatch,
):
    bridge = MuseMCPBridge(output_dir=tmp_path / "output")
    bridge.upload_dir = tmp_path / "uploads"
    bridge.upload_dir.mkdir(parents=True, exist_ok=True)

    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://files.example.test/ref.png"
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"fake-image-bytes",
        )

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "muse_ai.mcp_server.httpx.AsyncClient",
        client_factory,
    )

    files = [
        OpenAIFileParam(
            download_url="https://files.example.test/ref.png",
            file_id="file_123",
            mime_type="image/png",
            file_name="../../reference.png",
        )
    ]
    paths, request_dir = await bridge._download_openai_files(files)

    assert request_dir is not None
    assert len(paths) == 1
    assert paths[0].parent == request_dir
    assert paths[0].name == "01-reference.png"
    assert paths[0].read_bytes() == b"fake-image-bytes"


@pytest.mark.asyncio
async def test_chatgpt_file_param_rejects_non_https(tmp_path):
    bridge = MuseMCPBridge(output_dir=tmp_path / "output")
    bridge.upload_dir = tmp_path / "uploads"
    bridge.upload_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="HTTPS URL"):
        await bridge._download_openai_files(
            [
                OpenAIFileParam(
                    download_url="http://127.0.0.1/private.png",
                    file_id="file_bad",
                    mime_type="image/png",
                    file_name="private.png",
                )
            ]
        )


def test_public_media_registration_rewrites_video_url(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"fake-mp4")
    bridge = MuseMCPBridge(
        output_dir=tmp_path / "output",
        public_base_url="https://example.trycloudflare.com",
    )
    payload = bridge._expose_downloaded_files(
        {
            "ok": True,
            "videos": [
                {
                    "mime_type": "video/mp4",
                    "url": None,
                    "download_url": None,
                }
            ],
        },
        [media],
    )
    assert payload["public_urls"][0].startswith(
        "https://example.trycloudflare.com/media/"
    )
    assert payload["download_urls"][0].endswith("?download=1")
    assert payload["videos"][0]["url"] == payload["public_urls"][0]
    assert (
        payload["videos"][0]["download_url"]
        == payload["download_urls"][0]
    )
    assert "local_path" not in payload["files"][0]
    media_id = payload["public_urls"][0].rsplit("/", 1)[-1]
    assert bridge.media_path(media_id) == media.resolve()


def test_public_base_url_auto_allows_tunnel_host(tmp_path):
    server = build_server(
        state_dir=tmp_path / "state",
        output_dir=tmp_path / "output",
        public_base_url="https://example.trycloudflare.com",
    )
    security = server.settings.transport_security
    assert security is not None
    assert "example.trycloudflare.com" in security.allowed_hosts
    assert "https://example.trycloudflare.com" in security.allowed_origins
