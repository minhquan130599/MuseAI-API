  1 | # MuseAI-API
  2 | 
  3 | Unofficial Python client reconstructed from an authorized Muse.ai browser HAR capture.
  4 | 
  5 | It implements the Muse/Hatch private transport observed in that capture:
  6 | 
  7 | - Muse native OTP login and reusable cookie session
  8 | - VM wake + fresh Hatch admission bootstrap
  9 | - WebSocket connection to Hatch Noise ingress
 10 | - `Noise_XX_25519_AESGCM_SHA256`
 11 | - protobuf wire framing, fragmentation and stream multiplexing
 12 | - normal JSON RPC and NDJSON subscriptions
 13 | - `chat.stream`, `chat.history`, session discovery
 14 | - inline image attachments used by the Muse web client
 15 | - generated-video detection and MP4 download
 16 | - filesystem read/write/upload helpers matching the captured client
 17 | 
 18 | ## Install
 19 | 
 20 | From:
 21 | 
 22 | `E:\MCP\codexpro-main\Extensions\MuseAI-API`
 23 | 
 24 | run:
 25 | 
 26 | ```cmd
 27 | py -3.12 -m venv .venv
 28 | .venv\Scripts\activate
 29 | python -m pip install -U -e ".[dev]"
 30 | ```
 31 | 
 32 | ## 1. Login once
 33 | 
 34 | ```cmd
 35 | muse-ai login --email YOUR_MUSE_EMAIL
 36 | ```
 37 | 
 38 | Enter the OTP when prompted. Cookies are saved under `.muse-state/`.
 39 | No credential copied from the HAR is stored in this project.
 40 | 
 41 | ## 2. Verify Noise/Hatch
 42 | 
 43 | ```cmd
 44 | muse-ai ping
 45 | muse-ai model
 46 | muse-ai history
 47 | ```
 48 | 
 49 | ## 3. Generate video from text only
 50 | 
 51 | No image is required:
 52 | 
 53 | ```cmd
 54 | muse-ai generate ^
 55 |   --prompt "Create a 10-second vertical 9:16 cinematic video of a modern coffee shop at night, warm lighting, slow camera push-in, realistic motion, no text overlay." ^
 56 |   --output outputs ^
 57 |   --timeout 600
 58 | ```
 59 | 
 60 | The request sent to Muse contains a single text item:
 61 | 
 62 | ```json
 63 | {"items":[{"type":"text","text":"..."}]}
 64 | ```
 65 | 
 66 | Reference images are optional. To use them, add one or more `--image` arguments.
 67 | 
 68 | ## 4. Generate video from images
 69 | 
 70 | ```cmd
 71 | muse-ai generate ^
 72 |   --image "D:\images\01.jpg" ^
 73 |   --image "D:\images\02.jpg" ^
 74 |   --image "D:\images\03.jpg" ^
 75 |   --prompt "Create a 10 second 9:16 video using these images, with smooth realistic transitions." ^
 76 |   --output outputs ^
 77 |   --timeout 600
 78 | ```
 79 | 
 80 | The client:
 81 | 
 82 | 1. reads existing history as a baseline;
 83 | 2. encodes images as the same inline `type=image` records found in the Muse web bundle;
 84 | 3. sends `POST /chat/stream` over Noise;
 85 | 4. discovers the new session/thread;
 86 | 5. polls `/chat/history` while generation runs server-side;
 87 | 6. detects newly-created video media;
 88 | 7. downloads from the signed media URL when available, otherwise through `fs.raw`.
 89 | 
 90 | Muse may create more than one result. Use e.g. `--min-videos 2` if you explicitly want to wait for two.
 91 | 
 92 | ## Python API
 93 | 
 94 | ```python
 95 | import asyncio
 96 | from muse_ai import MuseAuth, MuseClient
 97 | 
 98 | async def main():
 99 |     auth = MuseAuth()
100 |     client = MuseClient(auth)
101 |     await client.connect()
102 |     try:
103 |         result = await client.generate_video(
104 |             images=["01.jpg", "02.jpg", "03.jpg"],
105 |             prompt="Create a 10 second vertical 9:16 video.",
106 |             output_dir="outputs",
107 |             timeout=600,
108 |         )
109 |         print(result.session_id)
110 |         print(result.downloaded)
111 |     finally:
112 |         await client.close()
113 | 
114 | asyncio.run(main())
115 | ```
116 | 
117 | ## Filesystem upload
118 | 
119 | The captured bundle writes large files in 512 KiB chunks. Multi-chunk files are first written to a sibling `.upload-<uuid>.part`, size-checked with `fs.stat`, then atomically published with `fs.rename`.
120 | 
121 | Test manually:
122 | 
123 | ```cmd
124 | muse-ai upload "D:\file.bin" "workspace/user/files/file.bin"
125 | ```
126 | 
127 | Image inputs do **not** use this upload path. The Muse web bundle sends images inline as base64; the client mirrors that and shrinks oversized images to keep the Noise request under its frame-assembly limit.
128 | 
129 | ## Protocol notes
130 | 
131 | Observed protocol:
132 | 
133 | ```text
134 | HTTP login/session
135 |   -> VM wake / Hatch bootstrap
136 |   -> wss://hatch.metaaivm.com/v1/noise
137 |   -> Noise XX handshake
138 |   -> encrypted NoiseTransportFrame chunks
139 |   -> ServiceRequest / ServiceFrame
140 |   -> ApplicationRequest
141 |   -> chat.stream / fs.* / model / ...
142 | ```
143 | 
144 | The Noise suite is:
145 | 
146 | ```text
147 | Noise_XX_25519_AESGCM_SHA256
148 | ```
149 | 
150 | Request streams are multiplexed by `stream_id`, so generation tracking and filesystem/history calls can run concurrently.
151 | 
152 | ## Security / stability
153 | 
154 | This is an **unofficial private API client** and Muse may change its protocol at any time. Use it only with an account you control and in accordance with the service terms.
155 | 
156 | The captured web client includes confidential-VM/SNP attestation verification logic. This implementation decrypts the standard-VM message-2 attestation payload but does not yet independently verify the SNP attestation chain. Do not treat this client as equivalent to the browser's confidential-VM trust verification.
157 | 
158 | Run offline verification with:
159 | 
160 | ```cmd
161 | python -m compileall muse_ai tests
162 | pytest -q
163 | ```
164 | 