 1 | from __future__ import annotations
 2 | 
 3 | import base64
 4 | from pathlib import Path
 5 | 
 6 | from PIL import Image
 7 | 
 8 | from muse_ai.attachments import build_image_item, build_items
 9 | from muse_ai.filesystem import normalize_gateway_path
10 | from muse_ai.media import extract_session_ids, extract_video_refs
11 | from muse_ai.noise import CipherState, NoiseXXInitiator, PROTOCOL, SymmetricState, hkdf_noise, nonce12
12 | from muse_ai.wire import NoiseChunk, NoiseReassembler, split_noise_payload
13 | 
14 | 
15 | def test_cipher_state_roundtrip():
16 |     key = bytes(range(32))
17 |     tx = CipherState(key)
18 |     rx = CipherState(key)
19 |     ciphertext = tx.encrypt_with_ad(b"ad", b"hello")
20 |     assert rx.decrypt_with_ad(b"ad", ciphertext) == b"hello"
21 | 
22 | 
23 | def test_noise_nonce_big_endian():
24 |     assert nonce12(1) == b"\x00" * 11 + b"\x01"
25 |     assert nonce12(0x0102030405060708)[4:] == bytes.fromhex("0102030405060708")
26 | 
27 | 
28 | def test_hkdf_is_deterministic():
29 |     a = hkdf_noise(b"a" * 32, b"b" * 32)
30 |     b = hkdf_noise(b"a" * 32, b"b" * 32)
31 |     assert a == b
32 |     assert all(len(item) == 32 for item in a)
33 | 
34 | 
35 | def test_noise_protocol_name_initialization():
36 |     # Noise spec: protocol names <= HASHLEN are zero-padded to HASHLEN.
37 |     assert len(PROTOCOL) < 32
38 |     state = SymmetricState()
39 |     assert len(state.ck) == 32
40 |     assert len(state.h) == 32
41 | 
42 |     initiator = NoiseXXInitiator()
43 |     message1 = initiator.write_message1(b"")
44 |     assert len(message1) == 32
45 | 
46 | 
47 | def test_noise_fragment_reassembly():
48 |     raw = b"x" * 140_000
49 |     chunks = split_noise_payload(raw)
50 |     assert len(chunks) == 3
51 |     reassembler = NoiseReassembler()
52 |     result = None
53 |     for chunk in chunks:
54 |         result = reassembler.feed(chunk)
55 |     assert result == raw
56 | 
57 | 
58 | def test_gateway_path_normalization():
59 |     assert normalize_gateway_path(r"C:\Users\alice\workspace\user\files\a.mp4") == "workspace/user/files/a.mp4"
60 |     assert normalize_gateway_path("/home/alice/workspace/user/files/a.mp4") == "workspace/user/files/a.mp4"
61 |     assert normalize_gateway_path("file:///workspace/user/files/a.mp4?x=1") == "workspace/user/files/a.mp4"
62 | 
63 | 
64 | def test_media_extraction_prefers_stable_path():
65 |     data = {
66 |         "session_id": "session-1",
67 |         "media": {
68 |             "kind": "video",
69 |             "mime_type": "video/mp4",
70 |             "path": "workspace/user/files/result.mp4",
71 |             "resource_id": "resource-1",
72 |             "media_handle": "media-handle-1",
73 |             "variants": {
74 |                 "original": "https://cdn.example.invalid/result.mp4?grant=one"
75 |             },
76 |         },
77 |     }
78 |     refs = extract_video_refs(data)
79 |     assert len(refs) == 1
80 |     assert refs[0].identity == "workspace/user/files/result.mp4"
81 |     assert refs[0].media_handle == "media-handle-1"
82 |     assert extract_session_ids(data) == ["session-1"]
83 | 
84 | 
85 | def test_build_inline_image_item(tmp_path: Path):
86 |     path = tmp_path / "image.png"
87 |     Image.new("RGB", (16, 16), (10, 20, 30)).save(path)
88 |     item = build_image_item(path)
89 |     assert item["type"] == "image"
90 |     assert item["mime_type"] == "image/png"
91 |     assert base64.b64decode(item["data_base64"]).startswith(b"\x89PNG")
92 | 
93 | 
94 | def test_build_text_only_items():
95 |     items = build_items([], "Create a cinematic 10-second video")
96 |     assert items == [
97 |         {"type": "text", "text": "Create a cinematic 10-second video"}
98 |     ]
99 | 