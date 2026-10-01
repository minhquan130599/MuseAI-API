from __future__ import annotations

import asyncio

import pytest

from muse_ai.client import GenerationResult, ImageGenerationResult, TextResult
from muse_ai.media import ImageRef, VideoRef
from muse_ai.mcp_server import (
    MuseMCPBridge,
    VideoJob,
    build_server,
    serialize_generation_result,
    serialize_image_generation_result,
    serialize_text_result,
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
    names = {tool.name for tool in tools}
    assert {
        "muse_auth_status",
        "muse_ping",
        "muse_model",
        "muse_send_text",
        "muse_history",
        "muse_generate_image",
        "muse_generate_video",
        "muse_job_status",
        "muse_list_jobs",
        "muse_cancel_job",
    } <= names

    bridge = server._muse_bridge
    await bridge.close()


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
