  1 | from __future__ import annotations
  2 | 
  3 | import asyncio
  4 | import json
  5 | import uuid
  6 | from dataclasses import dataclass, field
  7 | from typing import AsyncIterator
  8 | from urllib.parse import urlencode
  9 | 
 10 | import websockets
 11 | 
 12 | from .errors import MuseProtocolError, MuseRpcError
 13 | from .noise import CipherState, EMPTY_AD, NoiseXXInitiator, make_message1_payload
 14 | from .wire import (
 15 |     ApplicationRequest,
 16 |     ApplicationResponse,
 17 |     DecodedServiceFrame,
 18 |     Header,
 19 |     NoiseReassembler,
 20 |     SERVICE_AUTHD,
 21 |     SERVICE_DAEMON,
 22 |     SERVICE_SENTINEL,
 23 |     SERVICE_VAULT,
 24 |     decode_service_frame,
 25 |     decode_service_response,
 26 |     encode_service_frame_request,
 27 |     encode_service_request,
 28 |     split_noise_payload,
 29 | )
 30 | 
 31 | SERVICES = {
 32 |     "daemon": SERVICE_DAEMON,
 33 |     "sentinel": SERVICE_SENTINEL,
 34 |     "vault": SERVICE_VAULT,
 35 |     "authd": SERVICE_AUTHD,
 36 | }
 37 | 
 38 | 
 39 | @dataclass(slots=True)
 40 | class RpcResponse:
 41 |     status: int
 42 |     headers: list[Header]
 43 |     body: bytes
 44 | 
 45 |     def json(self):
 46 |         if not self.body:
 47 |             return None
 48 |         return json.loads(self.body.decode("utf-8"))
 49 | 
 50 |     def text(self) -> str:
 51 |         return self.body.decode("utf-8", "replace")
 52 | 
 53 |     def raise_for_status(self) -> "RpcResponse":
 54 |         if self.status >= 400:
 55 |             raise MuseRpcError(
 56 |                 f"Hatch RPC returned HTTP {self.status}: {self.text()[:500]}",
 57 |                 status=self.status,
 58 |                 body=self.body,
 59 |             )
 60 |         return self
 61 | 
 62 | 
 63 | class NoiseTransport:
 64 |     def __init__(self, tx: CipherState, rx: CipherState) -> None:
 65 |         self.tx = tx
 66 |         self.rx = rx
 67 |         self.next_stream_id = 1
 68 |         self.reassembler = NoiseReassembler()
 69 | 
 70 |     def encrypt_request(
 71 |         self,
 72 |         *,
 73 |         method: str,
 74 |         path: str,
 75 |         headers: list[Header],
 76 |         body: bytes,
 77 |         service: str = "daemon",
 78 |     ) -> tuple[int, list[bytes]]:
 79 |         stream_id = self.next_stream_id
 80 |         self.next_stream_id += 1
 81 |         app = ApplicationRequest(method, path, headers, body, True)
 82 |         service_frame = encode_service_frame_request(stream_id, app)
 83 |         envelope = encode_service_request(SERVICES[service], service_frame)
 84 |         encrypted = [
 85 |             self.tx.encrypt_with_ad(EMPTY_AD, chunk)
 86 |             for chunk in split_noise_payload(envelope)
 87 |         ]
 88 |         return stream_id, encrypted
 89 | 
 90 |     def decrypt_ws_frame(self, encrypted: bytes) -> DecodedServiceFrame | None:
 91 |         raw_chunk = self.rx.decrypt_with_ad(EMPTY_AD, encrypted)
 92 |         assembled = self.reassembler.feed(raw_chunk)
 93 |         if assembled is None:
 94 |             return None
 95 |         service_payload = decode_service_response(assembled)
 96 |         return decode_service_frame(service_payload)
 97 | 
 98 | 
 99 | @dataclass
100 | class _StreamState:
101 |     method: str
102 |     queue: asyncio.Queue = field(default_factory=asyncio.Queue)
103 | 
104 | 
105 | class HatchConnection:
106 |     def __init__(self, ws, transport: NoiseTransport, *, locale: str = "en-US") -> None:
107 |         self.ws = ws
108 |         self.transport = transport
109 |         self.locale = locale
110 |         self._streams: dict[int, _StreamState] = {}
111 |         self._send_lock = asyncio.Lock()
112 |         self._closed = asyncio.Event()
113 |         self._fatal: BaseException | None = None
114 |         self._receiver_task = asyncio.create_task(
115 |             self._receiver_loop(), name="muse-hatch-noise-receiver"
116 |         )
117 | 
118 |     @classmethod
119 |     async def connect(
120 |         cls,
121 |         ws_url: str,
122 |         *,
123 |         locale: str = "en-US",
124 |         timeout: float = 15.0,
125 |     ) -> "HatchConnection":
126 |         ws = await websockets.connect(
127 |             ws_url,
128 |             max_size=None,
129 |             open_timeout=timeout,
130 |             ping_interval=20,
131 |             ping_timeout=20,
132 |         )
133 |         noise = NoiseXXInitiator()
134 |         _client_nonce, payload1 = make_message1_payload()
135 |         await ws.send(noise.write_message1(payload1))
136 |         message2 = await asyncio.wait_for(ws.recv(), timeout)
137 |         if not isinstance(message2, (bytes, bytearray)):
138 |             await ws.close()
139 |             raise MuseProtocolError("expected binary Noise handshake message2")
140 |         # The captured primary VM flow is a standard VM: message2 carries attestation
141 |         # bytes but no owner recovery challenge.  We decrypt the attestation here.
142 |         # Full SNP attestation verification is intentionally a separate hardening step.
143 |         noise.read_message2(bytes(message2))
144 |         await ws.send(noise.write_message3(b""))
145 |         tx, rx = noise.split()
146 |         return cls(ws, NoiseTransport(tx, rx), locale=locale)
147 | 
148 |     def _headers(
149 |         self,
150 |         *,
151 |         json_body: bool,
152 |         extra: list[Header] | None = None,
153 |     ) -> list[Header]:
154 |         headers: list[Header] = []
155 |         if json_body:
156 |             headers.append(Header("Content-Type", "application/json"))
157 |         headers.extend(
158 |             [
159 |                 Header("x-request-id", str(uuid.uuid4())),
160 |                 Header("x-app-id", "hatch-web"),
161 |                 Header("Accept-Language", self.locale),
162 |             ]
163 |         )
164 |         if extra:
165 |             headers.extend(extra)
166 |         return headers
167 | 
168 |     async def _receiver_loop(self) -> None:
169 |         try:
170 |             async for message in self.ws:
171 |                 if not isinstance(message, (bytes, bytearray)):
172 |                     continue
173 |                 frame = self.transport.decrypt_ws_frame(bytes(message))
174 |                 if frame is None:
175 |                     continue
176 |                 state = self._streams.get(frame.stream_id)
177 |                 if state is not None:
178 |                     await state.queue.put(frame)
179 |         except asyncio.CancelledError:
180 |             raise
181 |         except BaseException as exc:
182 |             self._fatal = exc
183 |         finally:
184 |             self._closed.set()
185 |             for state in list(self._streams.values()):
186 |                 await state.queue.put(None)
187 | 
188 |     async def _open(
189 |         self,
190 |         method: str,
191 |         path: str,
192 |         *,
193 |         body: bytes,
194 |         json_body: bool,
195 |         service: str,
196 |         extra_headers: list[Header] | None,
197 |     ) -> tuple[int, _StreamState]:
198 |         if self._closed.is_set():
199 |             raise MuseProtocolError(f"Noise connection is closed: {self._fatal!r}")
200 |         async with self._send_lock:
201 |             stream_id, frames = self.transport.encrypt_request(
202 |                 method=method,
203 |                 path=path,
204 |                 headers=self._headers(json_body=json_body, extra=extra_headers),
205 |                 body=body,
206 |                 service=service,
207 |             )
208 |             state = _StreamState(method=method)
209 |             self._streams[stream_id] = state
210 |             try:
211 |                 for frame in frames:
212 |                     await self.ws.send(frame)
213 |             except BaseException:
214 |                 self._streams.pop(stream_id, None)
215 |                 raise
216 |         return stream_id, state
217 | 
218 |     @staticmethod
219 |     def _encode_request(
220 |         method: str, path: str, params: dict | None
221 |     ) -> tuple[str, bytes, bool]:
222 |         if method.upper() == "GET":
223 |             query = urlencode(params or {}, doseq=True)
224 |             if query:
225 |                 path = f"{path}{'&' if '?' in path else '?'}{query}"
226 |             return path, b"", False
227 |         body = json.dumps(
228 |             params or {}, ensure_ascii=False, separators=(",", ":")
229 |         ).encode("utf-8")
230 |         return path, body, True
231 | 
232 |     async def request(
233 |         self,
234 |         method: str,
235 |         path: str,
236 |         params: dict | None = None,
237 |         *,
238 |         service: str = "daemon",
239 |         extra_headers: list[Header] | None = None,
240 |         timeout: float = 60.0,
241 |     ) -> RpcResponse:
242 |         path, body, json_body = self._encode_request(method, path, params)
243 |         stream_id, state = await self._open(
244 |             method.upper(),
245 |             path,
246 |             body=body,
247 |             json_body=json_body,
248 |             service=service,
249 |             extra_headers=extra_headers,
250 |         )
251 |         status = 0
252 |         headers: list[Header] = []
253 |         chunks = bytearray()
254 |         try:
255 |             async with asyncio.timeout(timeout):
256 |                 while True:
257 |                     frame = await state.queue.get()
258 |                     if frame is None:
259 |                         raise MuseProtocolError(
260 |                             f"Noise connection closed during {state.method}: {self._fatal!r}"
261 |                         )
262 |                     if frame.kind == "reset":
263 |                         code, reason = frame.payload
264 |                         raise MuseRpcError(
265 |                             f"Hatch stream reset code={code}: {reason}"
266 |                         )
267 |                     if frame.kind == "response":
268 |                         response: ApplicationResponse = frame.payload
269 |                         status = response.status
270 |                         headers = response.headers or []
271 |                         chunks += response.body
272 |                         if response.end_body:
273 |                             break
274 |                     elif frame.kind == "body_chunk":
275 |                         data, end_body = frame.payload
276 |                         chunks += data
277 |                         if end_body:
278 |                             break
279 |         finally:
280 |             self._streams.pop(stream_id, None)
281 |         return RpcResponse(status=status, headers=headers, body=bytes(chunks))
282 | 
283 |     async def subscribe(
284 |         self,
285 |         method: str,
286 |         path: str,
287 |         params: dict | None = None,
288 |         *,
289 |         service: str = "daemon",
290 |         extra_headers: list[Header] | None = None,
291 |         timeout: float | None = None,
292 |     ) -> AsyncIterator[dict]:
293 |         path, body, json_body = self._encode_request(method, path, params)
294 |         stream_id, state = await self._open(
295 |             method.upper(),
296 |             path,
297 |             body=body,
298 |             json_body=json_body,
299 |             service=service,
300 |             extra_headers=extra_headers,
301 |         )
302 |         buffer = bytearray()
303 | 
304 |         async def next_frame():
305 |             if timeout is None:
306 |                 return await state.queue.get()
307 |             return await asyncio.wait_for(state.queue.get(), timeout)
308 | 
309 |         try:
310 |             while True:
311 |                 frame = await next_frame()
312 |                 if frame is None:
313 |                     raise MuseProtocolError(
314 |                         f"Noise connection closed during subscription: {self._fatal!r}"
315 |                     )
316 |                 if frame.kind == "reset":
317 |                     code, reason = frame.payload
318 |                     raise MuseRpcError(
319 |                         f"Hatch subscription reset code={code}: {reason}"
320 |                     )
321 |                 end_body = False
322 |                 if frame.kind == "response":
323 |                     response: ApplicationResponse = frame.payload
324 |                     if response.status >= 400:
325 |                         raise MuseRpcError(
326 |                             f"Hatch subscription HTTP {response.status}: "
327 |                             f"{response.body[:500]!r}",
328 |                             status=response.status,
329 |                             body=response.body,
330 |                         )
331 |                     buffer += response.body
332 |                     end_body = response.end_body
333 |                 elif frame.kind == "body_chunk":
334 |                     data, end_body = frame.payload
335 |                     buffer += data
336 |                 else:
337 |                     continue
338 | 
339 |                 while b"\n" in buffer:
340 |                     line, _, rest = buffer.partition(b"\n")
341 |                     buffer[:] = rest
342 |                     if line.strip():
343 |                         yield json.loads(line)
344 | 
345 |                 if end_body:
346 |                     if buffer.strip():
347 |                         yield json.loads(buffer)
348 |                     return
349 |         finally:
350 |             self._streams.pop(stream_id, None)
351 | 
352 |     async def close(self) -> None:
353 |         if not self._closed.is_set():
354 |             await self.ws.close()
355 |         if not self._receiver_task.done():
356 |             self._receiver_task.cancel()
357 |             try:
358 |                 await self._receiver_task
359 |             except asyncio.CancelledError:
360 |                 pass
361 | 