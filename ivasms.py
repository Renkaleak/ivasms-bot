import asyncio
import io
import json
import logging
import re
import urllib.parse
from datetime import date

import aiohttp
from curl_cffi.requests import AsyncSession as CurlSession

from flaresolverr import is_cf_challenge, get_saved_ua, FLARESOLVERR_URL, IVASMS_BASE_URL

logger = logging.getLogger(__name__)

SOCKET_URL = "https://ivasms.com:2087"

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

JSON_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


class CFBlockedError(Exception):
    pass


class SessionExpiredError(Exception):
    pass


class _MockResp:
    def __init__(self, status: int, body_text: str, url: str, body_bytes: bytes = None):
        self.status = status
        self._body_text = body_text or ""
        self._body_bytes = (
            body_bytes
            if body_bytes is not None
            else (body_text or "").encode("utf-8", errors="ignore")
        )
        self.url = url


def parse_cookies(raw: str) -> dict[str, str]:
    if not raw or not raw.strip():
        return {}
    stripped = raw.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k and v}
            if isinstance(data, list):
                return {
                    item["name"]: item["value"]
                    for item in data
                    if isinstance(item, dict) and item.get("name") and item.get("value")
                }
        except Exception:
            pass
    result = {}
    for part in stripped.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip():
                result[k.strip()] = v.strip()
    return result


def _xsrf_header(cookies: dict[str, str]) -> str:
    return urllib.parse.unquote(cookies.get("XSRF-TOKEN", ""))


def xlsx_bytes_to_numbers(data: bytes) -> list[str]:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb.active
        numbers = []
        for row in ws.iter_rows(min_row=4, values_only=True):
            if not row or len(row) < 2 or row[1] is None:
                continue
            try:
                numbers.append(str(int(float(str(row[1])))))
            except (ValueError, TypeError):
                s = str(row[1]).strip()
                if re.match(r"^\d{7,15}$", s):
                    numbers.append(s)
        wb.close()
        logger.info(f"xlsx_bytes_to_numbers: extracted {len(numbers)} numbers")
        return numbers
    except Exception as e:
        logger.error(f"xlsx_bytes_to_numbers error: {e}")
        return []


def numbers_to_txt(numbers: list[str]) -> bytes:
    return "\n".join(numbers).encode("utf-8")


def _timeout_seconds(t) -> int:
    if t is None:
        return 30
    if hasattr(t, "total") and t.total is not None:
        return int(t.total)
    if isinstance(t, (int, float)):
        return int(t)
    return 30


class IVASMSClient:
    def __init__(self, cookies_raw: str):
        self.cookies: dict[str, str] = parse_cookies(cookies_raw)
        self.csrf_token: str | None = None
        self.session: CurlSession | None = None
        self.user_agent: str = get_saved_ua()

    async def open(self):
        if self.session:
            return
        # chrome124 → better Cloudflare bypass via JA3/JA4 TLS fingerprinting
        self.session = CurlSession(impersonate="chrome124")

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *_):
        await self.close()

    def get_cookies_str(self) -> str:
        merged = dict(self.cookies)
        if self.session:
            for c in self.session.cookies:
                if c.name and c.value:
                    merged[c.name] = c.value
        return json.dumps(merged)

    # ------------------------------------------------------------------ #
    # Core request                                                         #
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        url: str,
        read_bytes: bool = False,
        headers: dict | None = None,
        data: dict | str | None = None,
        allow_redirects: bool = True,
        timeout=None,
    ) -> _MockResp:
        """
        Make an HTTP request via curl_cffi (Chrome TLS fingerprint).
        Cookies are passed directly in every request so they're always fresh.
        Falls back to FlareSolverr cookie refresh on genuine CF challenge.
        """
        timeout_s = _timeout_seconds(timeout)

        kw: dict = {
            "cookies": self.cookies,
            "allow_redirects": allow_redirects,
            "timeout": timeout_s,
        }
        if headers:
            kw["headers"] = headers
        if data is not None:
            kw["data"] = data

        for attempt in range(2):
            resp = await getattr(self.session, method)(url, **kw)

            status = resp.status_code
            if read_bytes:
                body_bytes = resp.content
                body_text = body_bytes.decode("utf-8", errors="ignore")
            else:
                body_text = resp.text
                body_bytes = body_text.encode("utf-8")

            # Absorb cookies set by server (curl_cffi cookies is a dict)
            for name, value in resp.cookies.items():
                if name and value:
                    self.cookies[name] = value
            kw["cookies"] = self.cookies  # keep kw fresh for retry

            if is_cf_challenge(body_text, status):
                logger.warning(
                    f"CF challenge at {url} | HTTP {status} | "
                    f"body[:150]: {body_text[:150].strip()!r}"
                )
                if attempt == 0:
                    await self._refresh_cf_via_flaresolverr()
                    kw["cookies"] = self.cookies
                    continue
                raise CFBlockedError(f"CF blocked after retry: {url}")

            logger.debug(
                f"{method.upper()} {url} → HTTP {status} | "
                f"body[:80]: {body_text[:80].strip()!r}"
            )
            return _MockResp(status, body_text, str(resp.url), body_bytes if read_bytes else None)

    async def _refresh_cf_via_flaresolverr(self):
        """Visit ivasms.com base URL via FlareSolverr to collect fresh CF cookies."""
        cookies_list = [{"name": k, "value": v} for k, v in self.cookies.items()]
        payload = {
            "cmd": "request.get",
            "url": IVASMS_BASE_URL,
            "cookies": cookies_list,
            "maxTimeout": 60000,
        }
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    FLARESOLVERR_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=90),
                ) as r:
                    data = await r.json(content_type=None)
            if data.get("status") == "ok":
                solution = data.get("solution", {})
                for c in solution.get("cookies", []):
                    n, v = c.get("name"), c.get("value")
                    if n and v:
                        self.cookies[n] = v
                ua = solution.get("userAgent")
                if ua:
                    self.user_agent = ua
                logger.info("CF cookies refreshed via FlareSolverr")
            else:
                logger.error(f"FlareSolverr fallback failed: {data.get('message')}")
        except Exception as e:
            logger.error(f"_refresh_cf_via_flaresolverr: {e}")

    # ------------------------------------------------------------------ #
    # Auth / session                                                        #
    # ------------------------------------------------------------------ #

    async def login(self) -> bool:
        try:
            resp = await self._request(
                "get",
                f"{IVASMS_BASE_URL}/portal/sms/received",
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=20),
            )
            final_url = str(resp.url)
            if "/login" in final_url:
                logger.error("login: redirected to /login — cookies are expired")
                return False
            if resp.status != 200:
                logger.error(f"login: HTTP {resp.status}")
                return False
            html = resp._body_text
            m = re.search(r'name="_token"\s+value="([^"]+)"', html)
            if m:
                self.csrf_token = m.group(1)
                logger.info("login OK — CSRF acquired")
                return True
            logger.error(f"login: CSRF not found | url={final_url} | body[:200]: {html[:200]!r}")
            return False
        except CFBlockedError as e:
            logger.error(f"login: {e}")
            return False
        except Exception as e:
            logger.error(f"login error: {e}")
            return False

    async def keepalive(self) -> bool:
        try:
            resp = await self._request(
                "get",
                f"{IVASMS_BASE_URL}/portal/dashboard",
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            if "/login" in str(resp.url):
                logger.warning("keepalive: session expired")
                return False
            m = re.search(r'name="_token"\s+value="([^"]+)"', resp._body_text)
            if m:
                self.csrf_token = m.group(1)
            logger.info("keepalive OK")
            return True
        except Exception as e:
            logger.error(f"keepalive: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Business logic                                                        #
    # ------------------------------------------------------------------ #

    async def get_wa_active_ranges(self, limit: int = 2000) -> list[dict]:
        xsrf = _xsrf_header(self.cookies)
        params = {
            "draw": "1", "start": "0", "length": str(limit),
            "columns[0][data]": "range", "columns[0][name]": "range",
            "columns[1][data]": "termination.test_number",
            "columns[1][name]": "termination.test_number",
            "columns[2][data]": "originator", "columns[2][name]": "originator",
            "columns[3][data]": "messagedata", "columns[3][name]": "messagedata",
            "columns[4][data]": "senttime", "columns[4][name]": "senttime",
            "order[0][column]": "4", "order[0][dir]": "desc",
            "search[value]": "WhatsApp", "search[regex]": "false",
        }
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{IVASMS_BASE_URL}/portal/sms/test/sms?{qs}"
        hdrs = {
            **JSON_HEADERS,
            "X-XSRF-TOKEN": xsrf,
            "Referer": f"{IVASMS_BASE_URL}/portal/sms/test/sms",
        }
        try:
            resp = await self._request(
                "get", url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=30)
            )
            if not resp._body_text.strip():
                logger.error(f"get_wa_active_ranges: empty response body | HTTP {resp.status}")
                return []
            data = json.loads(resp._body_text)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(
                f"get_wa_active_ranges JSON error: {e} | "
                f"body[:300]: {resp._body_text[:300] if 'resp' in dir() else 'N/A'!r}"
            )
            return []
        except Exception as e:
            logger.error(f"get_wa_active_ranges: {e}")
            return []

        rows = data.get("data", [])
        logger.info(f"get_wa_active_ranges: got {len(rows)} rows from API")
        range_map: dict[str, dict] = {}
        for r in rows:
            tid = r.get("termination_id")
            rng = r.get("range", "")
            sent = r.get("senttime", "")
            if not tid or not rng:
                continue
            key = str(tid)
            if key not in range_map:
                m = re.match(r"^(.*?)\s+(\d+)\s*$", rng.strip())
                range_map[key] = {
                    "range": rng.strip(),
                    "termination_id": tid,
                    "country": m.group(1).strip().title() if m else rng.strip(),
                    "range_num": m.group(2) if m else rng.strip(),
                    "count": 0,
                    "last_seen": sent,
                }
            range_map[key]["count"] += 1
            if sent > range_map[key]["last_seen"]:
                range_map[key]["last_seen"] = sent

        return sorted(range_map.values(), key=lambda x: x["count"], reverse=True)

    async def add_range(self, termination_id: int | str, max_retry: int = 3) -> dict:
        if not self.csrf_token:
            return {"ok": False, "message": "No CSRF token — call login() first"}
        payload_data = {"_token": self.csrf_token, "id": str(termination_id)}
        hdrs = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{IVASMS_BASE_URL}/portal/sms/test/sms",
        }
        for attempt in range(max_retry + 1):
            try:
                resp = await self._request(
                    "post",
                    f"{IVASMS_BASE_URL}/portal/numbers/termination/number/add",
                    data=payload_data,
                    headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=15),
                )
                if resp.status == 429:
                    wait = 5 * (attempt + 1)
                    logger.warning(f"add_range 429 — waiting {wait}s")
                    if attempt < max_retry:
                        await asyncio.sleep(wait)
                        continue
                    return {"ok": False, "message": "HTTP 429 — rate limited"}
                j = json.loads(resp._body_text)
                msg = j.get("message", "OK")
                return {"ok": "error" not in msg.lower(), "message": msg}
            except Exception as e:
                logger.error(f"add_range({termination_id}): {e}")
                return {"ok": False, "message": str(e)}
        return {"ok": False, "message": "Max retry exceeded"}

    async def bulk_return_all(self) -> dict:
        if not self.csrf_token:
            return {"ok": False, "count": 0, "message": "No CSRF token"}
        xsrf = _xsrf_header(self.cookies)
        hdrs = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN": xsrf,
            "Referer": f"{IVASMS_BASE_URL}/portal/numbers",
        }
        try:
            resp = await self._request(
                "post",
                f"{IVASMS_BASE_URL}/portal/numbers/return/allnumber/bluck",
                data={"_token": self.csrf_token},
                headers=hdrs,
                timeout=aiohttp.ClientTimeout(total=30),
            )
            j = json.loads(resp._body_text)
            msg = j.get("message", "")
            count = j.get("count", 0)
            return {"ok": "successfully" in msg.lower() or count > 0, "count": count, "message": msg}
        except Exception as e:
            logger.error(f"bulk_return_all: {e}")
            return {"ok": False, "count": 0, "message": str(e)}

    async def download_xlsx(self) -> bytes | None:
        try:
            resp = await self._request(
                "get",
                f"{IVASMS_BASE_URL}/portal/numbers/export",
                read_bytes=True,
                headers={"Referer": f"{IVASMS_BASE_URL}/portal/numbers"},
                timeout=aiohttp.ClientTimeout(total=60),
                allow_redirects=True,
            )
            if resp.status != 200 or len(resp._body_bytes) < 100:
                return None
            return resp._body_bytes
        except Exception as e:
            logger.error(f"download_xlsx: {e}")
            return None

    async def get_my_numbers_count(self) -> int:
        xsrf = _xsrf_header(self.cookies)
        params = (
            "draw=1&start=0&length=1"
            "&columns[0][data]=number_id&columns[0][name]=id"
            "&columns[1][data]=Number&columns[1][name]=number"
            "&columns[2][data]=range&columns[2][name]=range"
            "&search[value]=&search[regex]=false"
        )
        try:
            resp = await self._request(
                "get",
                f"{IVASMS_BASE_URL}/portal/numbers?{params}",
                headers={**JSON_HEADERS, "X-XSRF-TOKEN": xsrf},
                timeout=aiohttp.ClientTimeout(total=15),
            )
            d = json.loads(resp._body_text)
            return int(d.get("recordsTotal", -1))
        except Exception as e:
            logger.error(f"get_my_numbers_count: {e}")
            return -1

    async def get_live_sms_socket_params(self) -> dict | None:
        try:
            resp = await self._request(
                "get",
                f"{IVASMS_BASE_URL}/portal/live/my_sms",
                timeout=aiohttp.ClientTimeout(total=20),
            )
            html = resp._body_text
        except Exception as e:
            logger.error(f"get_live_sms_socket_params: {e}")
            return None

        m_token = re.search(r"token:\s*'([^']+)'", html)
        m_user = re.search(r'user:\s*"([a-f0-9]{32})"', html)
        m_event = re.search(r'liveSMSSocket\.on\("([A-Za-z0-9+/]+=*)"', html)

        if not m_token or not m_user or not m_event:
            logger.error("get_live_sms_socket_params: regex failed")
            return None

        return {
            "token": m_token.group(1),
            "user": m_user.group(1),
            "event_name": m_event.group(1),
        }

    async def get_received_sms_today(self) -> list[dict]:
        if not self.csrf_token:
            return []
        today = date.today().strftime("%Y-%m-%d")
        hdrs = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{IVASMS_BASE_URL}/portal/sms/received",
        }
        try:
            resp = await self._request(
                "post",
                f"{IVASMS_BASE_URL}/portal/sms/received/getsms",
                data={"from": today, "to": today, "_token": self.csrf_token},
                headers=hdrs,
                timeout=aiohttp.ClientTimeout(total=20),
            )
            html = resp._body_text
            results = []

            def strip_tags(s):
                return re.sub(r"<[^>]+>", "", s).strip()

            for row in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", html, re.IGNORECASE):
                cells = re.findall(r"<td[^>]*>([\s\S]*?)</td>", row, re.IGNORECASE)
                if len(cells) >= 3:
                    results.append({
                        "number": strip_tags(cells[0]),
                        "originator": strip_tags(cells[1]) if len(cells) > 1 else "",
                        "message": strip_tags(cells[2]) if len(cells) > 2 else "",
                        "time": strip_tags(cells[3]) if len(cells) > 3 else "",
                    })
            return results
        except Exception as e:
            logger.error(f"get_received_sms_today: {e}")
            return []
