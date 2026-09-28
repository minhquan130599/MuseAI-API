  1 | from __future__ import annotations
  2 | 
  3 | import html as html_lib
  4 | import json
  5 | import re
  6 | import uuid
  7 | from dataclasses import dataclass, replace
  8 | from pathlib import Path
  9 | from urllib.parse import urlencode, urlparse
 10 | 
 11 | import httpx
 12 | from curl_cffi.requests import AsyncSession as BrowserAsyncSession
 13 | 
 14 | from .errors import MuseAuthError
 15 | 
 16 | 
 17 | @dataclass(slots=True)
 18 | class MuseBootstrap:
 19 |     gateway_url: str
 20 |     vm_name: str
 21 |     admission: str
 22 |     notary: str | None = None
 23 |     locale: str = "en-US"
 24 | 
 25 |     @property
 26 |     def vm_id(self) -> str:
 27 |         if self.vm_name:
 28 |             return self.vm_name
 29 |         host = urlparse(self.gateway_url).hostname or ""
 30 |         return host.split(".", 1)[0]
 31 | 
 32 |     def websocket_url(self) -> str:
 33 |         params = {
 34 |             "vm_id": self.vm_id,
 35 |             "auth_" + "token": self.admission,
 36 |             "app_id": "hatch-web",
 37 |             "request_id": str(uuid.uuid4()),
 38 |         }
 39 |         if self.notary:
 40 |             params["notary_" + "token"] = self.notary
 41 |         return "wss://hatch.metaaivm.com/v1/noise?" + urlencode(params)
 42 | 
 43 | 
 44 | class MuseAuth:
 45 |     BASE = "https://muse.ai"
 46 | 
 47 |     def __init__(self, *, state_dir: str | Path = ".muse-state", timeout: float = 30.0) -> None:
 48 |         self.state_dir = Path(state_dir)
 49 |         self.cookie_path = self.state_dir / "cookies.json"
 50 |         self.waterfall_id: str | None = None
 51 |         self.csrf_token: str | None = None
 52 |         self.login_referer = self.BASE + "/"
 53 |         self.browser = BrowserAsyncSession(
 54 |             impersonate="chrome",
 55 |             timeout=timeout,
 56 |             headers={
 57 |                 "Accept-Language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
 58 |             },
 59 |         )
 60 |         self.http = httpx.AsyncClient(
 61 |             base_url=self.BASE,
 62 |             follow_redirects=True,
 63 |             timeout=timeout,
 64 |             headers={
 65 |                 "User-Agent": (
 66 |                     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
 67 |                     "AppleWebKit/537.36 (KHTML, like Gecko) "
 68 |                     "Chrome/153.0.0.0 Safari/537.36"
 69 |                 ),
 70 |                 "Accept-Language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
 71 |                 "Sec-CH-UA": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
 72 |                 "Sec-CH-UA-Mobile": "?0",
 73 |                 "Sec-CH-UA-Platform": '"Windows"',
 74 |             },
 75 |         )
 76 | 
 77 |     def _browser_cookie_names(self) -> list[str]:
 78 |         return sorted({cookie.name for cookie in self.browser.cookies.jar})
 79 | 
 80 |     def _sync_browser_to_httpx(self) -> None:
 81 |         for cookie in self.browser.cookies.jar:
 82 |             self.http.cookies.set(
 83 |                 cookie.name,
 84 |                 cookie.value,
 85 |                 domain=cookie.domain,
 86 |                 path=cookie.path or "/",
 87 |             )
 88 | 
 89 |     async def prime_browser_session(self) -> None:
 90 |         # The first AYM/H navigation is fingerprint-sensitive.  Use curl_cffi's
 91 |         # Chrome impersonation for the complete cross-site redirect cycle.
 92 |         response = await self.browser.get(
 93 |             self.BASE + "/",
 94 |             allow_redirects=True,
 95 |         )
 96 |         if response.status_code >= 400:
 97 |             raise MuseAuthError(
 98 |                 "Muse browser bootstrap failed: "
 99 |                 f"HTTP {response.status_code} at {response.url}: {response.text[:500]}"
100 |             )
101 |         self.login_referer = str(response.url)
102 |         if "aymh_complete=1" not in self.login_referer:
103 |             raise MuseAuthError(
104 |                 "Muse AYM/H redirect cycle did not complete; "
105 |                 f"final_url={self.login_referer!r}, cookies={self._browser_cookie_names()}"
106 |             )
107 |         if "datr" not in self._browser_cookie_names():
108 |             raise MuseAuthError(
109 |                 "Muse browser bootstrap completed without the datr cookie; "
110 |                 "the AYM/H flow likely changed."
111 |             )
112 |         self._sync_browser_to_httpx()
113 |         try:
114 |             await self.browser.get(
115 |                 self.BASE + "/api/consent/status",
116 |                 headers={"Accept": "*/*", "Referer": self.login_referer},
117 |             )
118 |         except Exception:
119 |             pass
120 |         self._sync_browser_to_httpx()
121 | 
122 |     def _flow_headers(self, *, include_csrf: bool = False) -> dict[str, str]:
123 |         headers = {
124 |             "Accept": "*/*",
125 |             "Origin": self.BASE,
126 |             "Referer": self.login_referer,
127 |             "Sec-Fetch-Site": "same-origin",
128 |             "Sec-Fetch-Mode": "cors",
129 |             "Sec-Fetch-Dest": "empty",
130 |         }
131 |         if self.waterfall_id:
132 |             headers["x-hatch-waterfall-id"] = self.waterfall_id
133 |         if include_csrf:
134 |             if not self.csrf_token:
135 |                 raise MuseAuthError("native auth CSRF token is missing; restart login first")
136 |             headers["x-hatch-csrf-token"] = self.csrf_token
137 |             headers["x-hatch-caa-reg-entry-point"] = "login_home"
138 |         return headers
139 | 
140 |     async def restart_login(self) -> dict:
141 |         self.waterfall_id = str(uuid.uuid4())
142 |         self.csrf_token = None
143 |         await self.prime_browser_session()
144 |         response = await self.browser.post(
145 |             self.BASE + "/api/auth/native/restart",
146 |             json={},
147 |             headers=self._flow_headers(),
148 |         )
149 |         if response.status_code >= 400:
150 |             cookie_names = self._browser_cookie_names()
151 |             raise MuseAuthError(
152 |                 "Muse native auth restart failed: "
153 |                 f"HTTP {response.status_code}: {response.text[:500]} "
154 |                 f"(referer={self.login_referer!r}, cookies={cookie_names})"
155 |             )
156 |         data = response.json()
157 |         token = data.get("csrf_token")
158 |         if not isinstance(token, str) or not token:
159 |             raise MuseAuthError("Muse native auth restart did not return csrf_token")
160 |         self.csrf_token = token
161 |         self._sync_browser_to_httpx()
162 |         # The web app's subsequent send-otp / confirm-otp requests use the root
163 |         # document as Referer rather than the aymh_complete navigation URL.
164 |         self.login_referer = self.BASE + "/"
165 |         return data
166 | 
167 |     async def send_otp(self, contact_point: str, region: str = "VN") -> dict:
168 |         response = await self.browser.post(
169 |             self.BASE + "/api/auth/native/send-otp",
170 |             json={"contact_point": contact_point, "region": region},
171 |             headers=self._flow_headers(include_csrf=True),
172 |         )
173 |         if response.status_code >= 400:
174 |             raise MuseAuthError(
175 |                 f"Muse send OTP failed: HTTP {response.status_code}: {response.text[:500]}"
176 |             )
177 |         self._sync_browser_to_httpx()
178 |         return response.json()
179 | 
180 |     async def confirm_otp(self, otp_code: str) -> dict:
181 |         response = await self.browser.post(
182 |             self.BASE + "/api/auth/native/confirm-otp",
183 |             json={"otp_code": otp_code},
184 |             headers=self._flow_headers(include_csrf=True),
185 |         )
186 |         if response.status_code >= 400:
187 |             raise MuseAuthError(
188 |                 f"Muse OTP failed: HTTP {response.status_code}: {response.text[:500]}"
189 |             )
190 |         self._sync_browser_to_httpx()
191 |         return response.json()
192 | 
193 |     async def save_account(self) -> dict:
194 |         response = await self.browser.post(
195 |             self.BASE + "/api/auth/save-account",
196 |             headers={
197 |                 "Accept": "*/*",
198 |                 "Origin": self.BASE,
199 |                 "Referer": self.BASE + "/",
200 |                 "Sec-Fetch-Site": "same-origin",
201 |                 "Sec-Fetch-Mode": "cors",
202 |                 "Sec-Fetch-Dest": "empty",
203 |             },
204 |         )
205 |         self._sync_browser_to_httpx()
206 |         if response.status_code >= 400:
207 |             raise MuseAuthError(
208 |                 f"Muse save-account failed: HTTP {response.status_code}: "
209 |                 f"{response.text[:500]}"
210 |             )
211 |         try:
212 |             return response.json()
213 |         except ValueError:
214 |             return {"saved": response.status_code < 400}
215 | 
216 |     async def login_interactive(
217 |         self,
218 |         contact_point: str,
219 |         *,
220 |         region: str = "VN",
221 |         otp_code: str | None = None,
222 |     ) -> dict:
223 |         await self.restart_login()
224 |         await self.send_otp(contact_point, region)
225 |         code = otp_code or input("Muse OTP: ").strip()
226 |         result = await self.confirm_otp(code)
227 |         await self.save_account()
228 |         check = await self.auth_check()
229 |         if check.get("ok") is not True:
230 |             raise MuseAuthError(
231 |                 "Muse login completed but account validation failed: "
232 |                 f"{check}"
233 |             )
234 |         await self.save_cookies()
235 |         return result
236 | 
237 |     async def auth_check(self) -> dict:
238 |         response = await self.browser.post(
239 |             self.BASE + "/api/auth/check",
240 |             headers={
241 |                 "Accept": "*/*",
242 |                 "Origin": self.BASE,
243 |                 "Referer": self.BASE + "/thread/new",
244 |                 "Sec-Fetch-Site": "same-origin",
245 |                 "Sec-Fetch-Mode": "cors",
246 |                 "Sec-Fetch-Dest": "empty",
247 |             },
248 |         )
249 |         self._sync_browser_to_httpx()
250 |         if response.status_code >= 400:
251 |             return {
252 |                 "ok": False,
253 |                 "status": response.status_code,
254 |                 "detail": response.text[:500],
255 |             }
256 |         try:
257 |             data = response.json()
258 |         except ValueError:
259 |             return {
260 |                 "ok": False,
261 |                 "status": response.status_code,
262 |                 "detail": "non-JSON response",
263 |             }
264 |         if "ok" not in data:
265 |             data["ok"] = response.status_code < 400 and bool(
266 |                 data.get("viewer_id") or data.get("access_token")
267 |             )
268 |         return data
269 | 
270 |     async def ensure_session(
271 |         self,
272 |         *,
273 |         contact_point: str | None = None,
274 |         region: str = "VN",
275 |         otp_code: str | None = None,
276 |     ) -> None:
277 |         if self.cookie_path.exists():
278 |             await self.load_cookies()
279 |             check = await self.auth_check()
280 |             if check.get("ok") is True:
281 |                 return
282 | 
283 |             # Older versions of this client saved hatch_sess immediately after OTP
284 |             # but skipped the browser's /api/auth/save-account commit.  Try to repair
285 |             # that saved session before forcing a new OTP flow.
286 |             try:
287 |                 saved = await self.save_account()
288 |                 if saved.get("saved") is True:
289 |                     check = await self.auth_check()
290 |                     if check.get("ok") is True:
291 |                         await self.save_cookies()
292 |                         return
293 |             except Exception:
294 |                 pass
295 |         if not contact_point:
296 |             raise MuseAuthError(
297 |                 "No valid saved Muse session. Run: muse-ai login --email <email>"
298 |             )
299 |         await self.login_interactive(contact_point, region=region, otp_code=otp_code)
300 |         check = await self.auth_check()
301 |         if check.get("ok") is not True:
302 |             raise MuseAuthError("Muse login completed but the session did not validate.")
303 | 
304 |     @staticmethod
305 |     def _find_string(text: str, field: str, required: bool = True) -> str | None:
306 |         backslash = chr(92)
307 |         normalized = html_lib.unescape(text).replace(backslash + '"', '"').replace(backslash + "/", "/")
308 |         match = re.search(
309 |             rf'"{re.escape(field)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
310 |             normalized,
311 |         )
312 |         if not match:
313 |             if required:
314 |                 raise MuseAuthError(f"bootstrap field {field!r} not found")
315 |             return None
316 |         raw = match.group(1)
317 |         try:
318 |             return json.loads('"' + raw + '"')
319 |         except Exception:
320 |             return raw
321 | 
322 |     def _muse_api_headers(self, *, referer: str | None = None) -> dict[str, str]:
323 |         return {
324 |             "Accept": "*/*",
325 |             "Origin": self.BASE,
326 |             "Referer": referer or self.BASE + "/",
327 |             "Sec-Fetch-Site": "same-origin",
328 |             "Sec-Fetch-Mode": "cors",
329 |             "Sec-Fetch-Dest": "empty",
330 |         }
331 | 
332 |     async def bootstrap_page(self) -> MuseBootstrap:
333 |         response = await self.browser.get(
334 |             self.BASE + "/",
335 |             headers={
336 |                 "Accept": (
337 |                     "text/html,application/xhtml+xml,application/xml;q=0.9,"
338 |                     "image/avif,image/webp,*/*;q=0.8"
339 |                 ),
340 |             },
341 |             allow_redirects=True,
342 |         )
343 |         if response.status_code >= 400:
344 |             raise MuseAuthError(
345 |                 "Muse bootstrap page failed: "
346 |                 f"HTTP {response.status_code} at {response.url}: {response.text[:500]}"
347 |             )
348 |         self._sync_browser_to_httpx()
349 |         text = response.text
350 |         gateway_field = "gateway" + "Url"
351 |         vm_field = "vm" + "Name"
352 |         admission_field = "auth" + "Token"
353 |         notary_field = "notary" + "Token"
354 |         locale_field = "viewer" + "Locale"
355 |         return MuseBootstrap(
356 |             gateway_url=self._find_string(text, gateway_field) or "",
357 |             vm_name=self._find_string(text, vm_field) or "",
358 |             admission=self._find_string(text, admission_field) or "",
359 |             notary=self._find_string(text, notary_field, required=False),
360 |             locale=(
361 |                 self._find_string(text, locale_field, required=False) or "en-US"
362 |             ).replace("_", "-"),
363 |         )
364 | 
365 |     async def wake_vm(self, bootstrap: MuseBootstrap, retry_count: int = 0) -> dict:
366 |         response = await self.browser.post(
367 |             self.BASE + "/api/hatch/vm/wake",
368 |             json={
369 |                 "vm_id": bootstrap.vm_id,
370 |                 "retry_count": retry_count,
371 |                 "connect_attempt_id": str(uuid.uuid4()),
372 |             },
373 |             headers=self._muse_api_headers(),
374 |         )
375 |         self._sync_browser_to_httpx()
376 |         if response.status_code >= 400:
377 |             raise MuseAuthError(
378 |                 "Muse VM wake failed: "
379 |                 f"HTTP {response.status_code}: {response.text[:500]} "
380 |                 f"(cookies={self._browser_cookie_names()})"
381 |             )
382 |         return response.json()
383 | 
384 |     async def refresh_hatch_token(self, bootstrap: MuseBootstrap) -> MuseBootstrap:
385 |         response = await self.browser.post(
386 |             self.BASE + "/api/hatch/token",
387 |             json={
388 |                 "vmAddress": bootstrap.gateway_url,
389 |                 "vmName": bootstrap.vm_name,
390 |             },
391 |             headers=self._muse_api_headers(),
392 |         )
393 |         self._sync_browser_to_httpx()
394 |         if response.status_code >= 400:
395 |             raise MuseAuthError(
396 |                 "Muse Hatch token request failed: "
397 |                 f"HTTP {response.status_code}: {response.text[:500]}"
398 |             )
399 |         data = response.json()
400 |         admission = data.get("token") or bootstrap.admission
401 |         notary = data.get("notary_" + "token") or bootstrap.notary
402 |         if not admission:
403 |             raise MuseAuthError("Hatch admission credential is missing")
404 |         return replace(bootstrap, admission=admission, notary=notary)
405 | 
406 |     async def bootstrap_hatch(self, *, wake: bool = True) -> MuseBootstrap:
407 |         bootstrap = await self.bootstrap_page()
408 |         if wake:
409 |             await self.wake_vm(bootstrap)
410 |         return await self.refresh_hatch_token(bootstrap)
411 | 
412 |     async def save_cookies(self, path: str | Path | None = None) -> None:
413 |         target = Path(path) if path is not None else self.cookie_path
414 |         target.parent.mkdir(parents=True, exist_ok=True)
415 |         merged: dict[tuple[str, str, str], dict] = {}
416 |         for jar in (self.browser.cookies.jar, self.http.cookies.jar):
417 |             for cookie in jar:
418 |                 key = (cookie.name, cookie.domain or "", cookie.path or "/")
419 |                 merged[key] = {
420 |                     "name": cookie.name,
421 |                     "value": cookie.value,
422 |                     "domain": cookie.domain,
423 |                     "path": cookie.path or "/",
424 |                 }
425 |         target.write_text(
426 |             json.dumps(list(merged.values()), indent=2), encoding="utf-8"
427 |         )
428 | 
429 |     async def load_cookies(self, path: str | Path | None = None) -> None:
430 |         target = Path(path) if path is not None else self.cookie_path
431 |         data = json.loads(target.read_text(encoding="utf-8"))
432 |         for c in data:
433 |             domain = c.get("domain")
434 |             cookie_path = c.get("path", "/")
435 |             self.http.cookies.set(
436 |                 c["name"], c["value"], domain=domain, path=cookie_path
437 |             )
438 |             self.browser.cookies.set(
439 |                 c["name"], c["value"], domain=domain, path=cookie_path
440 |             )
441 | 
442 |     async def close(self) -> None:
443 |         await self.browser.close()
444 |         await self.http.aclose()
445 | 