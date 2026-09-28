  1 | from __future__ import annotations
  2 | 
  3 | from dataclasses import dataclass
  4 | import json
  5 | from typing import Any
  6 | 
  7 | 
  8 | @dataclass(frozen=True, slots=True)
  9 | class VideoRef:
 10 |     path: str | None = None
 11 |     url: str | None = None
 12 |     mime_type: str | None = None
 13 |     resource_id: str | None = None
 14 |     media_handle: str | None = None
 15 | 
 16 |     @property
 17 |     def identity(self) -> str:
 18 |         # Signed URLs can rotate while the generated file itself is unchanged.
 19 |         return (
 20 |             self.path
 21 |             or self.resource_id
 22 |             or self.media_handle
 23 |             or self.url
 24 |             or repr(self)
 25 |         )
 26 | 
 27 | 
 28 | def _is_video_mime(value: Any) -> bool:
 29 |     return isinstance(value, str) and (
 30 |         value.lower().startswith("video/") or value.lower() == "mp4"
 31 |     )
 32 | 
 33 | 
 34 | def _looks_video_path(value: Any) -> bool:
 35 |     if not isinstance(value, str):
 36 |         return False
 37 |     clean = value.lower().split("?", 1)[0].split("#", 1)[0]
 38 |     return clean.endswith((".mp4", ".webm", ".mov", ".m4v"))
 39 | 
 40 | 
 41 | def extract_session_ids(value: Any) -> list[str]:
 42 |     found: list[str] = []
 43 | 
 44 |     def walk(node: Any) -> None:
 45 |         if isinstance(node, dict):
 46 |             for key, item in node.items():
 47 |                 if key in {"session_id", "sessionId", "thread_id", "threadId"}:
 48 |                     if isinstance(item, str) and item and item not in found:
 49 |                         found.append(item)
 50 |                 walk(item)
 51 |         elif isinstance(node, list):
 52 |             for item in node:
 53 |                 walk(item)
 54 | 
 55 |     walk(value)
 56 |     return found
 57 | 
 58 | 
 59 | def extract_video_refs(value: Any) -> list[VideoRef]:
 60 |     found: dict[str, VideoRef] = {}
 61 | 
 62 |     def add(ref: VideoRef) -> None:
 63 |         if not (ref.path or ref.url or ref.media_handle):
 64 |             return
 65 | 
 66 |         aliases = {
 67 |             value
 68 |             for value in (ref.path, ref.url, ref.resource_id, ref.media_handle)
 69 |             if value
 70 |         }
 71 |         for existing_key, existing in list(found.items()):
 72 |             existing_aliases = {
 73 |                 value
 74 |                 for value in (
 75 |                     existing.path,
 76 |                     existing.url,
 77 |                     existing.resource_id,
 78 |                     existing.media_handle,
 79 |                 )
 80 |                 if value
 81 |             }
 82 |             if aliases & existing_aliases:
 83 |                 merged = VideoRef(
 84 |                     path=ref.path or existing.path,
 85 |                     url=ref.url or existing.url,
 86 |                     mime_type=ref.mime_type or existing.mime_type,
 87 |                     resource_id=ref.resource_id or existing.resource_id,
 88 |                     media_handle=ref.media_handle or existing.media_handle,
 89 |                 )
 90 |                 del found[existing_key]
 91 |                 found[merged.identity] = merged
 92 |                 return
 93 | 
 94 |         found[ref.identity] = ref
 95 | 
 96 |     def walk(node: Any) -> None:
 97 |         if isinstance(node, dict):
 98 |             mime = (
 99 |                 node.get("mime_type")
100 |                 or node.get("mimeType")
101 |                 or node.get("mime")
102 |                 or node.get("content_type")
103 |                 or node.get("contentType")
104 |             )
105 |             kind = node.get("kind") or node.get("type")
106 |             path = node.get("path")
107 |             url = node.get("url")
108 |             variants = node.get("variants")
109 |             original = variants.get("original") if isinstance(variants, dict) else None
110 |             resource_id = node.get("resource_id") or node.get("resourceId")
111 |             media_handle = node.get("media_handle") or node.get("mediaHandle")
112 |             videoish = (
113 |                 kind == "video"
114 |                 or _is_video_mime(mime)
115 |                 or _looks_video_path(path)
116 |                 or _looks_video_path(url)
117 |                 or _looks_video_path(original)
118 |             )
119 |             if videoish:
120 |                 add(
121 |                     VideoRef(
122 |                         path=path if isinstance(path, str) else None,
123 |                         url=(
124 |                             original
125 |                             if isinstance(original, str)
126 |                             else url if isinstance(url, str) else None
127 |                         ),
128 |                         mime_type=mime if isinstance(mime, str) else None,
129 |                         resource_id=(
130 |                             resource_id if isinstance(resource_id, str) else None
131 |                         ),
132 |                         media_handle=(
133 |                             media_handle if isinstance(media_handle, str) else None
134 |                         ),
135 |                     )
136 |                 )
137 |             for item in node.values():
138 |                 walk(item)
139 |         elif isinstance(node, list):
140 |             for item in node:
141 |                 walk(item)
142 |         elif isinstance(node, str):
143 |             stripped = node.strip()
144 |             if stripped.startswith(("{", "[")):
145 |                 try:
146 |                     walk(json.loads(stripped))
147 |                     return
148 |                 except (ValueError, TypeError):
149 |                     pass
150 |             if _looks_video_path(node):
151 |                 if node.startswith(("http://", "https://")):
152 |                     add(VideoRef(url=node))
153 |                 else:
154 |                     add(VideoRef(path=node))
155 | 
156 |     walk(value)
157 |     return list(found.values())
158 | 