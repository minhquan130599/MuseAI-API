 1 | from __future__ import annotations
 2 | 
 3 | import base64
 4 | import io
 5 | import mimetypes
 6 | from pathlib import Path
 7 | 
 8 | from PIL import Image
 9 | 
10 | 
11 | def _mime_for(path: Path) -> str:
12 |     mime, _ = mimetypes.guess_type(path.name)
13 |     if mime and mime.startswith("image/"):
14 |         return mime
15 |     suffix = path.suffix.lower()
16 |     return {
17 |         ".jpg": "image/jpeg",
18 |         ".jpeg": "image/jpeg",
19 |         ".png": "image/png",
20 |         ".webp": "image/webp",
21 |         ".gif": "image/gif",
22 |     }.get(suffix, "application/octet-stream")
23 | 
24 | 
25 | def _prepare_image_bytes(
26 |     path: Path,
27 |     *,
28 |     max_dimension: int = 2048,
29 |     target_bytes: int = 3_500_000,
30 | ) -> tuple[bytes, str, str]:
31 |     raw = path.read_bytes()
32 |     mime = _mime_for(path)
33 |     if not mime.startswith("image/"):
34 |         raise ValueError(f"{path} is not a supported image")
35 | 
36 |     # The Muse web client sends image attachments inline as base64.  Browser-side
37 |     # preprocessing can shrink large images; mirror that behavior enough to keep
38 |     # the whole chat.stream request inside Noise's ~16 MiB assembly budget.
39 |     if len(raw) <= target_bytes:
40 |         return raw, mime, path.name
41 | 
42 |     with Image.open(io.BytesIO(raw)) as image:
43 |         image.load()
44 |         if max(image.size) > max_dimension:
45 |             image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
46 | 
47 |         has_alpha = image.mode in ("RGBA", "LA") or (
48 |             image.mode == "P" and "transparency" in image.info
49 |         )
50 |         if has_alpha:
51 |             out = io.BytesIO()
52 |             image.save(out, format="WEBP", quality=88, method=6)
53 |             data = out.getvalue()
54 |             return data, "image/webp", path.stem + ".webp"
55 | 
56 |         image = image.convert("RGB")
57 |         quality = 90
58 |         data = b""
59 |         while quality >= 55:
60 |             out = io.BytesIO()
61 |             image.save(out, format="JPEG", quality=quality, optimize=True)
62 |             data = out.getvalue()
63 |             if len(data) <= target_bytes:
64 |                 break
65 |             quality -= 8
66 |         return data, "image/jpeg", path.stem + ".jpg"
67 | 
68 | 
69 | def build_image_item(path: str | Path) -> dict:
70 |     source = Path(path)
71 |     if not source.is_file():
72 |         raise FileNotFoundError(source)
73 |     data, mime, filename = _prepare_image_bytes(source)
74 |     return {
75 |         "type": "image",
76 |         "mime_type": mime,
77 |         "data_base64": base64.b64encode(data).decode("ascii"),
78 |         "filename": filename,
79 |     }
80 | 
81 | 
82 | def build_items(images: list[str | Path], prompt: str) -> list[dict]:
83 |     items = [build_image_item(path) for path in images]
84 |     items.append({"type": "text", "text": prompt})
85 |     return items
86 | 