  1 | """Minimal protobuf wire codec for Muse Hatch Noise transport.
  2 | 
  3 | The wire shapes below are taken from the browser bundle captured by the user.  Keeping
  4 | this codec local avoids a protoc build-time dependency.
  5 | """
  6 | from __future__ import annotations
  7 | 
  8 | from dataclasses import dataclass
  9 | import os
 10 | import struct
 11 | from typing import Iterator
 12 | 
 13 | MAX_NOISE_CHUNK_PAYLOAD = 65_489
 14 | MAX_NOISE_CHUNKS = 256
 15 | MAX_ASSEMBLY_BYTES = 16 * 1024 * 1024
 16 | 
 17 | 
 18 | def varint(value: int) -> bytes:
 19 |     if value < 0:
 20 |         value &= (1 << 64) - 1
 21 |     out = bytearray()
 22 |     while True:
 23 |         b = value & 0x7F
 24 |         value >>= 7
 25 |         if value:
 26 |             out.append(b | 0x80)
 27 |         else:
 28 |             out.append(b)
 29 |             return bytes(out)
 30 | 
 31 | 
 32 | def key(field: int, wire_type: int) -> bytes:
 33 |     return varint((field << 3) | wire_type)
 34 | 
 35 | 
 36 | def f_varint(field: int, value: int) -> bytes:
 37 |     return key(field, 0) + varint(value)
 38 | 
 39 | 
 40 | def f_bytes(field: int, value: bytes) -> bytes:
 41 |     return key(field, 2) + varint(len(value)) + value
 42 | 
 43 | 
 44 | def f_string(field: int, value: str) -> bytes:
 45 |     return f_bytes(field, value.encode("utf-8"))
 46 | 
 47 | 
 48 | def f_bool(field: int, value: bool) -> bytes:
 49 |     return f_varint(field, 1 if value else 0)
 50 | 
 51 | 
 52 | def read_varint(data: bytes, pos: int = 0) -> tuple[int, int]:
 53 |     value = 0
 54 |     shift = 0
 55 |     while True:
 56 |         if pos >= len(data):
 57 |             raise ValueError("truncated varint")
 58 |         b = data[pos]
 59 |         pos += 1
 60 |         value |= (b & 0x7F) << shift
 61 |         if not b & 0x80:
 62 |             return value, pos
 63 |         shift += 7
 64 |         if shift > 70:
 65 |             raise ValueError("varint too long")
 66 | 
 67 | 
 68 | def fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
 69 |     pos = 0
 70 |     while pos < len(data):
 71 |         tag, pos = read_varint(data, pos)
 72 |         num, wire_type = tag >> 3, tag & 7
 73 |         if wire_type == 0:
 74 |             value, pos = read_varint(data, pos)
 75 |             yield num, wire_type, value
 76 |         elif wire_type == 2:
 77 |             length, pos = read_varint(data, pos)
 78 |             end = pos + length
 79 |             if end > len(data):
 80 |                 raise ValueError("truncated length-delimited field")
 81 |             yield num, wire_type, data[pos:end]
 82 |             pos = end
 83 |         elif wire_type == 1:
 84 |             end = pos + 8
 85 |             if end > len(data):
 86 |                 raise ValueError("truncated fixed64")
 87 |             yield num, wire_type, data[pos:end]
 88 |             pos = end
 89 |         elif wire_type == 5:
 90 |             end = pos + 4
 91 |             if end > len(data):
 92 |                 raise ValueError("truncated fixed32")
 93 |             yield num, wire_type, data[pos:end]
 94 |             pos = end
 95 |         else:
 96 |             raise ValueError(f"unsupported protobuf wire type {wire_type}")
 97 | 
 98 | 
 99 | def signed64(value: int) -> int:
100 |     return value - (1 << 64) if value & (1 << 63) else value
101 | 
102 | 
103 | @dataclass(slots=True)
104 | class Header:
105 |     key: str
106 |     value: str
107 | 
108 |     def encode(self) -> bytes:
109 |         return f_string(1, self.key) + f_string(2, self.value)
110 | 
111 | 
112 | @dataclass(slots=True)
113 | class ApplicationRequest:
114 |     verb: str
115 |     path: str
116 |     headers: list[Header]
117 |     body: bytes = b""
118 |     end_body: bool = True
119 | 
120 |     def encode(self) -> bytes:
121 |         out = bytearray()
122 |         out += f_string(1, self.verb)
123 |         out += f_string(2, self.path)
124 |         for header in self.headers:
125 |             out += f_bytes(3, header.encode())
126 |         if self.body:
127 |             out += f_bytes(4, self.body)
128 |         out += f_bool(5, self.end_body)
129 |         return bytes(out)
130 | 
131 | 
132 | @dataclass(slots=True)
133 | class ApplicationResponse:
134 |     status: int = 0
135 |     headers: list[Header] | None = None
136 |     body: bytes = b""
137 |     end_body: bool = False
138 | 
139 |     @classmethod
140 |     def decode(cls, data: bytes) -> "ApplicationResponse":
141 |         status = 0
142 |         headers: list[Header] = []
143 |         body = b""
144 |         end_body = False
145 |         for num, wire_type, value in fields(data):
146 |             if num == 1 and wire_type == 0:
147 |                 status = int(value)
148 |             elif num == 2 and wire_type == 2:
149 |                 hkey = hvalue = ""
150 |                 for n2, w2, v2 in fields(bytes(value)):
151 |                     if n2 == 1 and w2 == 2:
152 |                         hkey = bytes(v2).decode("utf-8", "replace")
153 |                     elif n2 == 2 and w2 == 2:
154 |                         hvalue = bytes(v2).decode("utf-8", "replace")
155 |                 headers.append(Header(hkey, hvalue))
156 |             elif num == 3 and wire_type == 2:
157 |                 body = bytes(value)
158 |             elif num == 4 and wire_type == 0:
159 |                 end_body = bool(value)
160 |         return cls(status=status, headers=headers, body=body, end_body=end_body)
161 | 
162 | 
163 | @dataclass(slots=True)
164 | class DecodedServiceFrame:
165 |     stream_id: int
166 |     kind: str
167 |     payload: object
168 | 
169 | 
170 | def encode_service_frame_request(stream_id: int, request: ApplicationRequest) -> bytes:
171 |     return f_varint(1, stream_id) + f_bytes(2, request.encode())
172 | 
173 | 
174 | def encode_service_frame_body(stream_id: int, data: bytes, end_body: bool = False) -> bytes:
175 |     chunk = f_bytes(1, data) + f_bool(2, end_body)
176 |     return f_varint(1, stream_id) + f_bytes(4, chunk)
177 | 
178 | 
179 | def decode_service_frame(data: bytes) -> DecodedServiceFrame:
180 |     stream_id = 0
181 |     kind = "unknown"
182 |     payload: object = None
183 |     for num, wire_type, value in fields(data):
184 |         if num == 1 and wire_type == 0:
185 |             stream_id = signed64(int(value))
186 |         elif num == 3 and wire_type == 2:
187 |             kind = "response"
188 |             payload = ApplicationResponse.decode(bytes(value))
189 |         elif num == 4 and wire_type == 2:
190 |             chunk_data = b""
191 |             end_body = False
192 |             for n2, w2, v2 in fields(bytes(value)):
193 |                 if n2 == 1 and w2 == 2:
194 |                     chunk_data = bytes(v2)
195 |                 elif n2 == 2 and w2 == 0:
196 |                     end_body = bool(v2)
197 |             kind = "body_chunk"
198 |             payload = (chunk_data, end_body)
199 |         elif num == 5 and wire_type == 2:
200 |             code = 0
201 |             reason = ""
202 |             for n2, w2, v2 in fields(bytes(value)):
203 |                 if n2 == 1 and w2 == 0:
204 |                     code = int(v2)
205 |                 elif n2 == 2 and w2 == 2:
206 |                     reason = bytes(v2).decode("utf-8", "replace")
207 |             kind = "reset"
208 |             payload = (code, reason)
209 |     return DecodedServiceFrame(stream_id=stream_id, kind=kind, payload=payload)
210 | 
211 | 
212 | SERVICE_DAEMON = 0
213 | SERVICE_SENTINEL = 1
214 | SERVICE_VAULT = 2
215 | SERVICE_AUTHD = 3
216 | 
217 | 
218 | def encode_service_request(service: int, service_frame: bytes) -> bytes:
219 |     return f_varint(1, service) + f_bytes(2, service_frame)
220 | 
221 | 
222 | def decode_service_response(data: bytes) -> bytes:
223 |     for num, wire_type, value in fields(data):
224 |         if num == 1 and wire_type == 2:
225 |             return bytes(value)
226 |     raise ValueError("ServiceResponse missing payload")
227 | 
228 | 
229 | @dataclass(slots=True)
230 | class NoiseChunk:
231 |     chunk_id: int
232 |     chunk_index: int
233 |     total_chunks: int
234 |     payload: bytes
235 | 
236 |     def encode(self) -> bytes:
237 |         return (
238 |             f_varint(1, self.chunk_id)
239 |             + f_varint(2, self.chunk_index)
240 |             + f_varint(3, self.total_chunks)
241 |             + f_bytes(4, self.payload)
242 |         )
243 | 
244 |     @classmethod
245 |     def decode(cls, data: bytes) -> "NoiseChunk":
246 |         chunk_id = 0
247 |         chunk_index = 0
248 |         total_chunks = 1
249 |         payload = b""
250 |         for num, wire_type, value in fields(data):
251 |             if num == 1 and wire_type == 0:
252 |                 chunk_id = signed64(int(value))
253 |             elif num == 2 and wire_type == 0:
254 |                 chunk_index = int(value)
255 |             elif num == 3 and wire_type == 0:
256 |                 total_chunks = int(value)
257 |             elif num == 4 and wire_type == 2:
258 |                 payload = bytes(value)
259 |         return cls(chunk_id, chunk_index, total_chunks, payload)
260 | 
261 | 
262 | def random_int64() -> int:
263 |     return struct.unpack("<q", os.urandom(8))[0]
264 | 
265 | 
266 | def split_noise_payload(data: bytes, max_payload: int = MAX_NOISE_CHUNK_PAYLOAD) -> list[bytes]:
267 |     total = max(1, (len(data) + max_payload - 1) // max_payload)
268 |     if total > MAX_NOISE_CHUNKS:
269 |         raise ValueError(f"payload too large for Noise framing: {total} chunks")
270 |     chunk_id = random_int64()
271 |     if not data:
272 |         return [NoiseChunk(chunk_id, 0, 1, b"").encode()]
273 |     return [
274 |         NoiseChunk(
275 |             chunk_id,
276 |             index,
277 |             total,
278 |             data[index * max_payload:(index + 1) * max_payload],
279 |         ).encode()
280 |         for index in range(total)
281 |     ]
282 | 
283 | 
284 | class NoiseReassembler:
285 |     def __init__(self) -> None:
286 |         self._pending: dict[int, tuple[int, dict[int, bytes], int]] = {}
287 | 
288 |     def feed(self, raw: bytes) -> bytes | None:
289 |         chunk = NoiseChunk.decode(raw)
290 |         if chunk.total_chunks < 1 or chunk.total_chunks > MAX_NOISE_CHUNKS:
291 |             raise ValueError("invalid total_chunks")
292 |         if chunk.chunk_index < 0 or chunk.chunk_index >= chunk.total_chunks:
293 |             raise ValueError("invalid chunk_index")
294 |         if len(chunk.payload) > MAX_NOISE_CHUNK_PAYLOAD:
295 |             raise ValueError("noise frame payload too large")
296 |         total, parts, size = self._pending.get(
297 |             chunk.chunk_id, (chunk.total_chunks, {}, 0)
298 |         )
299 |         if total != chunk.total_chunks:
300 |             self._pending.pop(chunk.chunk_id, None)
301 |             raise ValueError("inconsistent total_chunks")
302 |         if chunk.chunk_index in parts:
303 |             self._pending.pop(chunk.chunk_id, None)
304 |             raise ValueError("duplicate noise chunk")
305 |         parts[chunk.chunk_index] = chunk.payload
306 |         size += len(chunk.payload)
307 |         if size > MAX_ASSEMBLY_BYTES:
308 |             self._pending.pop(chunk.chunk_id, None)
309 |             raise ValueError("noise assembly exceeded 16 MiB")
310 |         self._pending[chunk.chunk_id] = (total, parts, size)
311 |         if len(parts) != total:
312 |             return None
313 |         self._pending.pop(chunk.chunk_id, None)
314 |         return b"".join(parts[index] for index in range(total))
315 | 