  1 | from __future__ import annotations
  2 | 
  3 | import argparse
  4 | import asyncio
  5 | import json
  6 | from pathlib import Path
  7 | 
  8 | from .auth import MuseAuth
  9 | from .client import MuseClient
 10 | 
 11 | 
 12 | def build_parser() -> argparse.ArgumentParser:
 13 |     parser = argparse.ArgumentParser(
 14 |         prog="muse-ai",
 15 |         description="Unofficial Muse.ai Hatch/Noise Python client",
 16 |     )
 17 |     parser.add_argument("--state-dir", default=".muse-state")
 18 |     sub = parser.add_subparsers(dest="command", required=True)
 19 | 
 20 |     login = sub.add_parser("login", help="Login with Muse OTP and save cookies")
 21 |     login.add_argument("--email", required=True)
 22 |     login.add_argument("--region", default="VN")
 23 | 
 24 |     sub.add_parser("ping", help="Connect to Hatch and call /api/ping")
 25 |     sub.add_parser("model", help="Read the active Muse model")
 26 | 
 27 |     history = sub.add_parser("history", help="Read recent chat history")
 28 |     history.add_argument("--session-id")
 29 |     history.add_argument("--limit", type=int, default=40)
 30 | 
 31 |     generate = sub.add_parser(
 32 |         "generate",
 33 |         help="Generate video from a text prompt, optionally with reference images",
 34 |     )
 35 |     generate.add_argument("--prompt", required=True)
 36 |     generate.add_argument(
 37 |         "--image",
 38 |         action="append",
 39 |         default=[],
 40 |         help="Optional reference image; repeat --image for multiple images",
 41 |     )
 42 |     generate.add_argument("--output", default="outputs")
 43 |     generate.add_argument("--session-id")
 44 |     generate.add_argument("--timeout", type=float, default=600)
 45 |     generate.add_argument("--min-videos", type=int, default=1)
 46 |     generate.add_argument("--no-download", action="store_true")
 47 | 
 48 |     upload = sub.add_parser("upload", help="Test the reverse-engineered fs.write upload")
 49 |     upload.add_argument("local_file")
 50 |     upload.add_argument("remote_path")
 51 | 
 52 |     return parser
 53 | 
 54 | 
 55 | async def _connect(args) -> MuseClient:
 56 |     auth = MuseAuth(state_dir=args.state_dir)
 57 |     client = MuseClient(auth, state_dir=args.state_dir)
 58 |     await client.connect()
 59 |     return client
 60 | 
 61 | 
 62 | async def run(args: argparse.Namespace) -> int:
 63 |     if args.command == "login":
 64 |         auth = MuseAuth(state_dir=args.state_dir)
 65 |         try:
 66 |             await auth.login_interactive(args.email, region=args.region)
 67 |             print(f"Login validated. Saved session in {auth.cookie_path}")
 68 |             return 0
 69 |         finally:
 70 |             await auth.close()
 71 | 
 72 |     client = await _connect(args)
 73 |     try:
 74 |         if args.command == "ping":
 75 |             response = await client.ping()
 76 |             print(f"HTTP {response.status}")
 77 |             print(response.text())
 78 |         elif args.command == "model":
 79 |             print(json.dumps(await client.model_get(), ensure_ascii=False, indent=2))
 80 |         elif args.command == "history":
 81 |             value = await client.history(
 82 |                 session_id=args.session_id,
 83 |                 limit=args.limit,
 84 |             )
 85 |             print(json.dumps(value, ensure_ascii=False, indent=2))
 86 |         elif args.command == "upload":
 87 |             remote = await client._fs().upload_file(
 88 |                 args.local_file,
 89 |                 args.remote_path,
 90 |                 stage_before_publish=True,
 91 |             )
 92 |             print(remote)
 93 |         elif args.command == "generate":
 94 |             result = await client.generate_video(
 95 |                 prompt=args.prompt,
 96 |                 images=[Path(value) for value in args.image] if args.image else [],
 97 |                 output_dir=args.output,
 98 |                 session_id=args.session_id,
 99 |                 timeout=args.timeout,
100 |                 min_videos=args.min_videos,
101 |                 download=not args.no_download,
102 |             )
103 |             print(f"session_id={result.session_id}")
104 |             for index, ref in enumerate(result.videos, start=1):
105 |                 print(
106 |                     f"video[{index}] path={ref.path!r} "
107 |                     f"url={ref.url!r} media_handle={ref.media_handle!r}"
108 |                 )
109 |             for path in result.downloaded:
110 |                 print(f"downloaded: {path}")
111 |             for error in result.download_errors:
112 |                 print(f"download warning: {error}")
113 |         return 0
114 |     finally:
115 |         await client.close()
116 | 
117 | 
118 | def main() -> None:
119 |     parser = build_parser()
120 |     args = parser.parse_args()
121 |     raise SystemExit(asyncio.run(run(args)))
122 | 
123 | 
124 | if __name__ == "__main__":
125 |     main()
126 | 