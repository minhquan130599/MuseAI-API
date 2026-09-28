 1 | from __future__ import annotations
 2 | 
 3 | from dataclasses import dataclass
 4 | 
 5 | 
 6 | @dataclass(frozen=True, slots=True)
 7 | class Route:
 8 |     http_method: str
 9 |     path: str
10 |     service: str = "daemon"
11 |     subscription: bool = False
12 |     binary: bool = False
13 | 
14 | 
15 | ROUTES: dict[str, Route] = {
16 |     "connection.ping": Route("POST", "/api/ping"),
17 |     "model.get": Route("GET", "/model"),
18 |     "client.register_capabilities": Route("POST", "/client/register-capabilities"),
19 |     "chat.stream": Route("POST", "/chat/stream", subscription=True),
20 |     "chat.stream_events": Route("POST", "/chat/stream-events", subscription=True),
21 |     "chat.subscribe": Route("POST", "/chat/subscribe", subscription=True),
22 |     "chat.history": Route("GET", "/chat/history"),
23 |     "chat.history_window": Route("GET", "/chat/history-window"),
24 |     "sessions.list": Route("GET", "/api/session/list"),
25 |     "fs.stats": Route("POST", "/fs/stats"),
26 |     "fs.list": Route("POST", "/fs/list"),
27 |     "fs.stat": Route("POST", "/fs/stat"),
28 |     "fs.read": Route("POST", "/fs/read"),
29 |     "fs.reads": Route("POST", "/fs/reads"),
30 |     "fs.write": Route("POST", "/fs/write"),
31 |     "fs.delete": Route("POST", "/fs/delete"),
32 |     "fs.rename": Route("POST", "/fs/rename"),
33 |     "fs.mkdir": Route("POST", "/fs/mkdir"),
34 |     "fs.library": Route("POST", "/fs/library"),
35 |     "fs.export": Route("POST", "/fs/export", binary=True),
36 |     "fs.subscribe": Route("POST", "/api/fs/subscribe", subscription=True),
37 | }
38 | 
39 | 
40 | CHAT_CAPABILITIES = [
41 |     "chat_cancel",
42 |     "delta_stream",
43 |     "custom_reactions",
44 |     "custom_reactions_facebook_thumbs_up_v1",
45 | ]
46 | 
47 | 
48 | def replay_subscribe_params(
49 |     after_stream_seq: int = 0,
50 |     after_chat_event_seq: int | None = None,
51 | ) -> dict:
52 |     if after_chat_event_seq is None:
53 |         after_chat_event_seq = after_stream_seq
54 |     return {
55 |         "after_stream_seq": max(0, int(after_stream_seq)),
56 |         "after_chat_event_seq": max(0, int(after_chat_event_seq)),
57 |         "capabilities": list(CHAT_CAPABILITIES),
58 |     }
59 | 