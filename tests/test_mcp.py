from __future__ import annotations

import asyncio

import pytest

from muse_ai.client import TextResult
from muse_ai.mcp_server import VideoJob, build_server, serialize_text_result
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


def test_video_job_public_does_not_serialize_asyncio_task():
    async def sleeper():
        await asyncio.sleep(0)

    async def scenario():
        task = asyncio.create_task(sleeper())
        job = VideoJob(id="job-1", prompt="create video", task=task)
        data = job.public()
        assert "task" not in data
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
        "muse_generate_video",
        "muse_job_status",
        "muse_list_jobs",
        "muse_cancel_job",
    } <= names

    bridge = server._muse_bridge
    await bridge.close()
