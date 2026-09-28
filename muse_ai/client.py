  1 | from __future__ import annotations
  2 | 
  3 | import asyncio
  4 | import json
  5 | import time
  6 | import uuid
  7 | from dataclasses import dataclass, field
  8 | from pathlib import Path
  9 | from urllib.parse import urlparse
 10 | 
 11 | from .attachments import build_items
 12 | from .auth import MuseAuth
 13 | from .filesystem import MuseFilesystem
 14 | from .media import VideoRef, extract_session_ids, extract_video_refs
 15 | from .routes import CHAT_CAPABILITIES
 16 | from .transport import HatchConnection, RpcResponse
 17 | 
 18 | 
 19 | @dataclass(slots=True)
 20 | class GenerationResult:
 21 |     session_id: str | None
 22 |     videos: list[VideoRef]
 23 |     downloaded: list[Path] = field(default_factory=list)
 24 |     download_errors: list[str] = field(default_factory=list)
 25 |     stream_events: list[dict] = field(default_factory=list)
 26 |     history: dict | list | None = None
 27 | 
 28 | 
 29 | class MuseClient:
 30 |     def __init__(
 31 |         self,
 32 |         auth: MuseAuth,
 33 |         *,
 34 |         state_dir: str | Path | None = None,
 35 |     ) -> None:
 36 |         self.auth = auth
 37 |         self.state_dir = Path(state_dir) if state_dir is not None else auth.state_dir
 38 |         self.conn: HatchConnection | None = None
 39 |         self.fs: MuseFilesystem | None = None
 40 | 
 41 |     async def connect(
 42 |         self,
 43 |         *,
 44 |         contact_point: str | None = None,
 45 |         region: str = "VN",
 46 |         otp_code: str | None = None,
 47 |     ) -> None:
 48 |         await self.auth.ensure_session(
 49 |             contact_point=contact_point,
 50 |             region=region,
 51 |             otp_code=otp_code,
 52 |         )
 53 |         bootstrap = await self.auth.bootstrap_hatch()
 54 |         self.conn = await HatchConnection.connect(
 55 |             bootstrap.websocket_url(),
 56 |             locale=bootstrap.locale,
 57 |         )
 58 |         self.fs = MuseFilesystem(self.conn)
 59 | 
 60 |     def _conn(self) -> HatchConnection:
 61 |         if self.conn is None:
 62 |             raise RuntimeError("MuseClient is not connected")
 63 |         return self.conn
 64 | 
 65 |     def _fs(self) -> MuseFilesystem:
 66 |         if self.fs is None:
 67 |             raise RuntimeError("MuseClient is not connected")
 68 |         return self.fs
 69 | 
 70 |     def device_id(self) -> str:
 71 |         path = self.state_dir / "device.json"
 72 |         if path.exists():
 73 |             try:
 74 |                 data = json.loads(path.read_text(encoding="utf-8"))
 75 |                 value = data.get("device_id")
 76 |                 if isinstance(value, str) and value:
 77 |                     return value
 78 |             except Exception:
 79 |                 pass
 80 |         value = str(uuid.uuid4())
 81 |         path.parent.mkdir(parents=True, exist_ok=True)
 82 |         path.write_text(json.dumps({"device_id": value}, indent=2), encoding="utf-8")
 83 |         return value
 84 | 
 85 |     async def ping(self) -> RpcResponse:
 86 |         response = await self._conn().request("POST", "/api/ping", {})
 87 |         return response.raise_for_status()
 88 | 
 89 |     async def model_get(self):
 90 |         response = await self._conn().request("GET", "/model", {})
 91 |         response.raise_for_status()
 92 |         return response.json()
 93 | 
 94 |     async def sessions_list(self):
 95 |         response = await self._conn().request("GET", "/api/session/list", {})
 96 |         response.raise_for_status()
 97 |         return response.json()
 98 | 
 99 |     async def history(
100 |         self,
101 |         *,
102 |         session_id: str | None = None,
103 |         limit: int = 40,
104 |         transcript_mode: str = "messages",
105 |     ):
106 |         params: dict = {"limit": limit, "transcript_mode": transcript_mode}
107 |         if session_id:
108 |             params["session_id"] = session_id
109 |         response = await self._conn().request("GET", "/chat/history", params)
110 |         response.raise_for_status()
111 |         return response.json()
112 | 
113 |     async def chat_stream(
114 |         self,
115 |         *,
116 |         prompt: str,
117 |         images: list[str | Path] | None = None,
118 |         session_id: str | None = None,
119 |         stream_timeout: float = 30.0,
120 |     ) -> list[dict]:
121 |         payload = {
122 |             "items": build_items(images or [], prompt),
123 |             "node_id": self.device_id(),
124 |             "capabilities": list(CHAT_CAPABILITIES),
125 |         }
126 |         if session_id:
127 |             payload["session_id"] = session_id
128 | 
129 |         events: list[dict] = []
130 |         try:
131 |             async with asyncio.timeout(stream_timeout):
132 |                 async for event in self._conn().subscribe(
133 |                     "POST",
134 |                     "/chat/stream",
135 |                     payload,
136 |                 ):
137 |                     events.append(event)
138 |         except TimeoutError:
139 |             # chat.stream is an acknowledgement/subscription endpoint.  Some server
140 |             # revisions leave it open longer than the generation itself; generation
141 |             # tracking below does not depend on it remaining open.
142 |             pass
143 |         return events
144 | 
145 |     async def _resolve_session_id(
146 |         self,
147 |         explicit: str | None,
148 |         events: list[dict],
149 |         sessions_before: set[str],
150 |     ) -> str | None:
151 |         if explicit:
152 |             return explicit
153 |         event_ids = extract_session_ids(events)
154 |         if event_ids:
155 |             return event_ids[-1]
156 |         try:
157 |             sessions_after = await self.sessions_list()
158 |             current = extract_session_ids(sessions_after)
159 |             new_ids = [value for value in current if value not in sessions_before]
160 |             if new_ids:
161 |                 return new_ids[0]
162 |             if current:
163 |                 return current[0]
164 |         except Exception:
165 |             pass
166 |         return None
167 | 
168 |     async def wait_for_videos(
169 |         self,
170 |         *,
171 |         session_id: str | None,
172 |         baseline: set[str],
173 |         timeout: float = 600.0,
174 |         poll_interval: float = 3.0,
175 |         settle_seconds: float = 5.0,
176 |         min_videos: int = 1,
177 |     ) -> tuple[list[VideoRef], dict | list | None]:
178 |         deadline = time.monotonic() + timeout
179 |         first_found_at: float | None = None
180 |         latest_history = None
181 |         latest_refs: list[VideoRef] = []
182 | 
183 |         while time.monotonic() < deadline:
184 |             latest_history = await self.history(session_id=session_id, limit=80)
185 |             refs = [
186 |                 ref
187 |                 for ref in extract_video_refs(latest_history)
188 |                 if ref.identity not in baseline
189 |             ]
190 |             unique = {ref.identity: ref for ref in refs}
191 |             latest_refs = list(unique.values())
192 |             if len(latest_refs) >= min_videos:
193 |                 if first_found_at is None:
194 |                     first_found_at = time.monotonic()
195 |                 if time.monotonic() - first_found_at >= settle_seconds:
196 |                     return latest_refs, latest_history
197 |             else:
198 |                 first_found_at = None
199 |             await asyncio.sleep(poll_interval)
200 | 
201 |         if latest_refs:
202 |             return latest_refs, latest_history
203 |         raise TimeoutError(
204 |             f"No new Muse video appeared within {timeout:.0f}s"
205 |             + (f" for session {session_id}" if session_id else "")
206 |         )
207 | 
208 |     @staticmethod
209 |     def _filename_for(ref: VideoRef, index: int) -> str:
210 |         candidate = ref.path
211 |         if not candidate and ref.url:
212 |             candidate = urlparse(ref.url).path
213 |         if candidate:
214 |             name = Path(candidate).name
215 |             if name and "." in name:
216 |                 return name
217 |         return f"muse_video_{index + 1}.mp4"
218 | 
219 |     async def download_video(
220 |         self,
221 |         ref: VideoRef,
222 |         destination: str | Path,
223 |     ) -> Path:
224 |         target = Path(destination)
225 |         target.parent.mkdir(parents=True, exist_ok=True)
226 |         attempts: list[str] = []
227 | 
228 |         async def write_bytes(data: bytes) -> Path:
229 |             target.write_bytes(data)
230 |             return target
231 | 
232 |         # The frontend treats /idea-cards/media/<handle> as a Hatch RPC media
233 |         # route, not a normal public HTTP URL.
234 |         if ref.url and "/idea-cards/media/" in ref.url:
235 |             try:
236 |                 return await write_bytes(
237 |                     await self._fs().idea_media_from_url(ref.url)
238 |                 )
239 |             except Exception as exc:
240 |                 attempts.append(f"variants.original idea-media: {exc}")
241 | 
242 |         # Some presentations expose the opaque media handle separately.
243 |         if ref.media_handle:
244 |             try:
245 |                 return await write_bytes(
246 |                     await self._fs().idea_media(ref.media_handle)
247 |                 )
248 |             except Exception as exc:
249 |                 attempts.append(f"media_handle: {exc}")
250 | 
251 |         # A true external/signed URL can be fetched directly.
252 |         if ref.url and ref.url.startswith(("http://", "https://")):
253 |             try:
254 |                 response = await self.auth.browser.get(
255 |                     ref.url,
256 |                     allow_redirects=True,
257 |                 )
258 |                 if response.status_code >= 400:
259 |                     raise RuntimeError(
260 |                         f"HTTP {response.status_code}: {response.text[:200]}"
261 |                     )
262 |                 return await write_bytes(response.content)
263 |             except Exception as exc:
264 |                 attempts.append(f"direct URL: {exc}")
265 | 
266 |         # variants.original can also be another sandbox workspace reference.
267 |         if ref.url and ref.url.startswith("sandbox://"):
268 |             try:
269 |                 return await write_bytes(await self._fs().raw(ref.url))
270 |             except Exception as exc:
271 |                 attempts.append(f"variants.original fs.raw: {exc}")
272 | 
273 |         # Last fallback: the presentation's sandbox path.  This is not always
274 |         # backed by /fs/raw, which is why media_handle is attempted first.
275 |         if ref.path:
276 |             try:
277 |                 return await self._fs().download(ref.path, target)
278 |             except Exception as exc:
279 |                 attempts.append(f"path fs.raw: {exc}")
280 | 
281 |         detail = "; ".join(attempts) if attempts else "no usable media source"
282 |         raise RuntimeError(f"unable to download generated video: {detail}")
283 | 
284 |     async def generate_video(
285 |         self,
286 |         *,
287 |         prompt: str,
288 |         images: list[str | Path] | None = None,
289 |         output_dir: str | Path = "outputs",
290 |         session_id: str | None = None,
291 |         timeout: float = 600.0,
292 |         min_videos: int = 1,
293 |         download: bool = True,
294 |     ) -> GenerationResult:
295 |         if not prompt.strip():
296 |             raise ValueError("video generation requires a non-empty prompt")
297 | 
298 |         images = images or []
299 |         sessions_before: set[str] = set()
300 |         if session_id is None:
301 |             try:
302 |                 sessions_before = set(extract_session_ids(await self.sessions_list()))
303 |             except Exception:
304 |                 pass
305 | 
306 |         baseline_history = await self.history(session_id=session_id, limit=80)
307 |         baseline = {ref.identity for ref in extract_video_refs(baseline_history)}
308 | 
309 |         events = await self.chat_stream(
310 |             prompt=prompt,
311 |             images=images,
312 |             session_id=session_id,
313 |         )
314 |         resolved_session = await self._resolve_session_id(
315 |             session_id, events, sessions_before
316 |         )
317 | 
318 |         videos, latest_history = await self.wait_for_videos(
319 |             session_id=resolved_session,
320 |             baseline=baseline,
321 |             timeout=timeout,
322 |             min_videos=min_videos,
323 |         )
324 | 
325 |         downloaded: list[Path] = []
326 |         download_errors: list[str] = []
327 |         if download:
328 |             out = Path(output_dir)
329 |             out.mkdir(parents=True, exist_ok=True)
330 |             used: set[str] = set()
331 |             for index, ref in enumerate(videos):
332 |                 filename = self._filename_for(ref, index)
333 |                 original = filename
334 |                 suffix = 1
335 |                 while filename in used or (out / filename).exists():
336 |                     stem = Path(original).stem
337 |                     ext = Path(original).suffix or ".mp4"
338 |                     filename = f"{stem}_{suffix}{ext}"
339 |                     suffix += 1
340 |                 used.add(filename)
341 |                 try:
342 |                     downloaded.append(
343 |                         await self.download_video(ref, out / filename)
344 |                     )
345 |                 except Exception as exc:
346 |                     download_errors.append(
347 |                         f"video[{index + 1}] {ref.identity}: {exc}"
348 |                     )
349 | 
350 |         return GenerationResult(
351 |             session_id=resolved_session,
352 |             videos=videos,
353 |             downloaded=downloaded,
354 |             download_errors=download_errors,
355 |             stream_events=events,
356 |             history=latest_history,
357 |         )
358 | 
359 |     async def close(self) -> None:
360 |         if self.conn is not None:
361 |             await self.conn.close()
362 |             self.conn = None
363 |             self.fs = None
364 |         await self.auth.close()
365 | 
366 |     async def __aenter__(self) -> "MuseClient":
367 |         await self.connect()
368 |         return self
369 | 
370 |     async def __aexit__(self, exc_type, exc, tb) -> None:
371 |         await self.close()
372 | 