from __future__ import annotations

import base64
from pathlib import Path

import pytest
from PIL import Image

from muse_ai.client import MuseClient

from muse_ai.attachments import build_image_item, build_items
from muse_ai.filesystem import normalize_gateway_path
from muse_ai.media import extract_image_refs, extract_session_ids, extract_video_refs
from muse_ai.noise import CipherState, NoiseXXInitiator, PROTOCOL, SymmetricState, hkdf_noise, nonce12
from muse_ai.wire import NoiseChunk, NoiseReassembler, split_noise_payload


def test_cipher_state_roundtrip():
    key = bytes(range(32))
    tx = CipherState(key)
    rx = CipherState(key)
    ciphertext = tx.encrypt_with_ad(b"ad", b"hello")
    assert rx.decrypt_with_ad(b"ad", ciphertext) == b"hello"


def test_noise_nonce_big_endian():
    assert nonce12(1) == b"\x00" * 11 + b"\x01"
    assert nonce12(0x0102030405060708)[4:] == bytes.fromhex("0102030405060708")


def test_hkdf_is_deterministic():
    a = hkdf_noise(b"a" * 32, b"b" * 32)
    b = hkdf_noise(b"a" * 32, b"b" * 32)
    assert a == b
    assert all(len(item) == 32 for item in a)


def test_noise_protocol_name_initialization():
    # Noise spec: protocol names <= HASHLEN are zero-padded to HASHLEN.
    assert len(PROTOCOL) < 32
    state = SymmetricState()
    assert len(state.ck) == 32
    assert len(state.h) == 32

    initiator = NoiseXXInitiator()
    message1 = initiator.write_message1(b"")
    assert len(message1) == 32


def test_noise_fragment_reassembly():
    raw = b"x" * 140_000
    chunks = split_noise_payload(raw)
    assert len(chunks) == 3
    reassembler = NoiseReassembler()
    result = None
    for chunk in chunks:
        result = reassembler.feed(chunk)
    assert result == raw


def test_gateway_path_normalization():
    assert normalize_gateway_path(r"C:\Users\alice\workspace\user\files\a.mp4") == "workspace/user/files/a.mp4"
    assert normalize_gateway_path("/home/alice/workspace/user/files/a.mp4") == "workspace/user/files/a.mp4"
    assert normalize_gateway_path("file:///workspace/user/files/a.mp4?x=1") == "workspace/user/files/a.mp4"


def test_media_extraction_prefers_stable_path():
    data = {
        "session_id": "session-1",
        "media": {
            "kind": "video",
            "mime_type": "video/mp4",
            "path": "workspace/user/files/result.mp4",
            "resource_id": "resource-1",
            "media_handle": "media-handle-1",
            "variants": {
                "original": "https://cdn.example.invalid/result.mp4?grant=one"
            },
        },
    }
    refs = extract_video_refs(data)
    assert len(refs) == 1
    assert refs[0].identity == "workspace/user/files/result.mp4"
    assert refs[0].media_handle == "media-handle-1"
    assert extract_session_ids(data) == ["session-1"]


def test_extract_generated_image_presentation():
    data = {
        "session_id": "session-image-1",
        "presentations": [
            {
                "kind": "image",
                "presentationId": "presentation-1",
                "data": {
                    "images": [
                        {
                            "path": "sandbox://workspace/user/files/generated.png",
                            "label": "generated.png",
                            "mime": "image/png",
                            "media_handle": "image-handle-1",
                            "width": 1024,
                            "height": 1024,
                            "variants": {
                                "original": "/api/idea-cards/media/image-handle-1"
                            },
                        }
                    ]
                },
            }
        ],
    }
    refs = extract_image_refs(data)
    assert len(refs) == 1
    assert refs[0].path == "sandbox://workspace/user/files/generated.png"
    assert refs[0].media_handle == "image-handle-1"
    assert refs[0].mime_type == "image/png"
    assert refs[0].label == "generated.png"
    assert refs[0].width == 1024
    assert refs[0].height == 1024
    assert refs[0].url == "/api/idea-cards/media/image-handle-1"


def test_build_inline_image_item(tmp_path: Path):
    path = tmp_path / "image.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(path)
    item = build_image_item(path)
    assert item["type"] == "image"
    assert item["mime_type"] == "image/png"
    assert base64.b64decode(item["data_base64"]).startswith(b"\x89PNG")


def test_build_text_only_items():
    items = build_items([], "Create a cinematic 10-second video")
    assert items == [
        {"type": "text", "text": "Create a cinematic 10-second video"}
    ]

@pytest.mark.asyncio
async def test_send_text_starts_new_secondary_thread_not_primary(tmp_path, monkeypatch):
    client = MuseClient(auth=None, state_dir=tmp_path)
    sent_sessions = []
    history_sessions = []

    async def fake_stream(*, prompt, session_id, stream_timeout):
        assert session_id
        sent_sessions.append(session_id)
        return [{"session_id": session_id, "is_thread": True, "channel": "ack"}]

    async def fake_history(*, session_id=None, limit=80, **kwargs):
        history_sessions.append(session_id)
        if len(history_sessions) in (1, 3):
            return {"messages": []}
        return {
            "messages": [
                {"role": "assistant", "content": "Chào bạn!"}
            ]
        }

    async def fail_session_list():
        raise AssertionError("No primary-session fallback is allowed")

    monkeypatch.setattr(client, "chat_stream", fake_stream)
    monkeypatch.setattr(client, "history", fake_history)
    monkeypatch.setattr(client, "sessions_list", fail_session_list)

    result = await client.send_text(prompt="xin chào", session_id=None, timeout=5)
    assert result.text == "Chào bạn!"
    assert result.session_id == sent_sessions[0]
    assert history_sessions == [sent_sessions[0], sent_sessions[0]]

    # An existing thread must be reused, not replaced with a fresh ID.
    second = await client.send_text(
        prompt="tiếp tục", session_id=result.session_id, timeout=5
    )
    assert second.session_id == result.session_id
    assert sent_sessions == [result.session_id, result.session_id]


@pytest.mark.asyncio
async def test_send_text_rejects_wrong_thread_ack(tmp_path, monkeypatch):
    client = MuseClient(auth=None, state_dir=tmp_path)

    async def fake_stream(*, prompt, session_id, stream_timeout):
        return [{"session_id": "existing-primary-thread", "is_primary": True}]

    async def fake_history(*, session_id=None, limit=80, **kwargs):
        return {"messages": []}

    monkeypatch.setattr(client, "chat_stream", fake_stream)
    monkeypatch.setattr(client, "history", fake_history)

    with pytest.raises(RuntimeError, match="different session_id"):
        await client.send_text(prompt="new chat", session_id=None, timeout=5)


@pytest.mark.asyncio
async def test_resolve_session_id_never_uses_arbitrary_existing_thread(
    tmp_path, monkeypatch
):
    client = MuseClient(auth=None, state_dir=tmp_path)

    async def fake_list():
        return {"result": {"sessions": [
            {"session_id": "primary-existing", "is_primary": True}
        ]}}

    monkeypatch.setattr(client, "sessions_list", fake_list)
    resolved = await client._resolve_session_id(
        explicit=None, events=[], sessions_before={"primary-existing"}
    )
    assert resolved is None

