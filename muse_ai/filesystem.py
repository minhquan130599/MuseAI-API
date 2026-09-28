  1 | from __future__ import annotations
  2 | 
  3 | import base64
  4 | import math
  5 | import uuid
  6 | from pathlib import Path
  7 | from urllib.parse import parse_qsl, quote, urlparse
  8 | 
  9 | from .transport import HatchConnection
 10 | from .wire import Header
 11 | 
 12 | UPLOAD_CHUNK_SIZE = 524_288
 13 | 
 14 | 
 15 | def normalize_gateway_path(value: str) -> str:
 16 |     text = value.strip()
 17 |     text = text.split("?", 1)[0].split("#", 1)[0]
 18 |     if text.startswith("file://"):
 19 |         text = text[7:]
 20 |     text = text.replace("\\", "/")
 21 |     while "//" in text:
 22 |         text = text.replace("//", "/")
 23 |     text = text.lstrip("/")
 24 |     parts = [part for part in text.split("/") if part]
 25 |     if not parts:
 26 |         return ""
 27 |     if "workspace" in parts:
 28 |         parts = parts[parts.index("workspace"):]
 29 |     elif parts[0] in {"home", "Users"} and len(parts) >= 3:
 30 |         parts = parts[2:]
 31 |     return "/".join(parts)
 32 | 
 33 | 
 34 | class MuseFilesystem:
 35 |     def __init__(self, conn: HatchConnection) -> None:
 36 |         self.conn = conn
 37 | 
 38 |     async def stat(self, path: str) -> dict:
 39 |         response = await self.conn.request(
 40 |             "POST", "/fs/stat", {"path": normalize_gateway_path(path)}
 41 |         )
 42 |         response.raise_for_status()
 43 |         return response.json()
 44 | 
 45 |     async def list(self, path: str) -> dict:
 46 |         response = await self.conn.request(
 47 |             "POST", "/fs/list", {"path": normalize_gateway_path(path)}
 48 |         )
 49 |         response.raise_for_status()
 50 |         return response.json()
 51 | 
 52 |     async def write_chunk(
 53 |         self,
 54 |         path: str,
 55 |         data: bytes,
 56 |         *,
 57 |         overwrite: bool,
 58 |         append: bool,
 59 |         create_parent: bool,
 60 |     ) -> dict | None:
 61 |         response = await self.conn.request(
 62 |             "POST",
 63 |             "/fs/write",
 64 |             {
 65 |                 "path": normalize_gateway_path(path),
 66 |                 "data_base64": base64.b64encode(data).decode("ascii"),
 67 |                 "overwrite": overwrite,
 68 |                 "append": append,
 69 |                 "create_parent": create_parent,
 70 |             },
 71 |             timeout=120,
 72 |         )
 73 |         response.raise_for_status()
 74 |         return response.json()
 75 | 
 76 |     async def rename(self, source: str, destination: str) -> dict | None:
 77 |         response = await self.conn.request(
 78 |             "POST",
 79 |             "/fs/rename",
 80 |             {
 81 |                 "source": normalize_gateway_path(source),
 82 |                 "destination": normalize_gateway_path(destination),
 83 |                 "create_parent": True,
 84 |                 "overwrite": True,
 85 |             },
 86 |         )
 87 |         response.raise_for_status()
 88 |         return response.json()
 89 | 
 90 |     async def delete(self, path: str, *, recursive: bool = False) -> dict | None:
 91 |         response = await self.conn.request(
 92 |             "POST",
 93 |             "/fs/delete",
 94 |             {"path": normalize_gateway_path(path), "recursive": recursive},
 95 |         )
 96 |         response.raise_for_status()
 97 |         return response.json()
 98 | 
 99 |     async def upload_file(
100 |         self,
101 |         local_path: str | Path,
102 |         remote_path: str,
103 |         *,
104 |         stage_before_publish: bool = True,
105 |     ) -> str:
106 |         source = Path(local_path)
107 |         size = source.stat().st_size
108 |         destination = normalize_gateway_path(remote_path)
109 |         total_chunks = math.ceil(size / UPLOAD_CHUNK_SIZE)
110 |         staged = stage_before_publish and total_chunks > 1
111 |         if "/" in destination:
112 |             parent, name = destination.rsplit("/", 1)
113 |             temp = f"{parent}/.upload-{uuid.uuid4()}.part"
114 |         else:
115 |             temp = f".upload-{uuid.uuid4()}.part"
116 |         target = temp if staged else destination
117 | 
118 |         try:
119 |             if size == 0:
120 |                 await self.write_chunk(
121 |                     target,
122 |                     b"",
123 |                     overwrite=True,
124 |                     append=False,
125 |                     create_parent=True,
126 |                 )
127 |             else:
128 |                 with source.open("rb") as handle:
129 |                     index = 0
130 |                     while True:
131 |                         chunk = handle.read(UPLOAD_CHUNK_SIZE)
132 |                         if not chunk:
133 |                             break
134 |                         await self.write_chunk(
135 |                             target,
136 |                             chunk,
137 |                             overwrite=index == 0,
138 |                             append=index != 0,
139 |                             create_parent=index == 0,
140 |                         )
141 |                         index += 1
142 | 
143 |             if staged:
144 |                 info = await self.stat(temp)
145 |                 if info.get("kind") != "file" or int(info.get("size", -1)) != size:
146 |                     raise RuntimeError("uploaded file failed size validation")
147 |                 await self.rename(temp, destination)
148 |             return destination
149 |         except BaseException:
150 |             if staged:
151 |                 try:
152 |                     await self.delete(temp)
153 |                 except Exception:
154 |                     pass
155 |             raise
156 | 
157 |     async def raw(
158 |         self,
159 |         path: str,
160 |         *,
161 |         byte_range: tuple[int, int] | None = None,
162 |         timeout: float = 300,
163 |     ) -> bytes:
164 |         normalized = normalize_gateway_path(path)
165 |         route = "/fs/raw/" + quote(normalized, safe="/")
166 |         headers: list[Header] = []
167 |         if byte_range is not None:
168 |             start, end = byte_range
169 |             if start < 0 or end < start:
170 |                 raise ValueError("invalid byte range")
171 |             headers.append(Header("Range", f"bytes={start}-{end}"))
172 |         response = await self.conn.request(
173 |             "GET",
174 |             route,
175 |             {},
176 |             extra_headers=headers,
177 |             timeout=timeout,
178 |         )
179 |         response.raise_for_status()
180 |         return response.body
181 | 
182 |     async def idea_media(
183 |         self,
184 |         handle: str,
185 |         *,
186 |         params: dict[str, str] | None = None,
187 |         timeout: float = 300,
188 |     ) -> bytes:
189 |         route = "/api/idea-cards/media/" + quote(handle, safe="")
190 |         response = await self.conn.request(
191 |             "GET",
192 |             route,
193 |             params or {},
194 |             timeout=timeout,
195 |         )
196 |         response.raise_for_status()
197 |         return response.body
198 | 
199 |     async def idea_media_from_url(self, value: str, *, timeout: float = 300) -> bytes:
200 |         parsed = urlparse(value)
201 |         path = parsed.path if parsed.scheme else value.split("?", 1)[0]
202 |         marker = "/idea-cards/media/"
203 |         if marker not in path:
204 |             raise ValueError("not an idea-cards media URL")
205 |         handle = path.split(marker, 1)[1].strip("/")
206 |         if not handle:
207 |             raise ValueError("idea-cards media handle is empty")
208 |         query = parsed.query if parsed.scheme else (
209 |             value.split("?", 1)[1] if "?" in value else ""
210 |         )
211 |         params = dict(parse_qsl(query, keep_blank_values=True))
212 |         return await self.idea_media(handle, params=params, timeout=timeout)
213 | 
214 |     async def download(self, path: str, destination: str | Path) -> Path:
215 |         target = Path(destination)
216 |         target.parent.mkdir(parents=True, exist_ok=True)
217 |         target.write_bytes(await self.raw(path))
218 |         return target
219 | 