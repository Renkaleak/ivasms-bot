import asyncio
import io
import json
import logging
import re
import urllib.parse
from datetime import date

import aiohttp

from flaresolverr import is_cf_challenge, solve_challenge, get_saved_ua

logger = logging.getLogger(__name__)

IVASMS_BASE_URL = "https://www.ivasms.com"
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


class IVASMSClient:
    def __init__(self, cookies_raw: str):
        self.cookies: dict[str, str] = parse_cookies(cookies_raw)
        self.csrf_token: str | None = None
        self.session: aiohttp.ClientSession | None = None
        self.user_agent: str = get_saved_ua()

    async def open(self):
        if self.session and not self.session.closed:
            return
        headers = {**DEFAULT_HEADERS, "User-Agent": self.user_agent}
        self.session = aiohttp.ClientSession(headers=headers)
        self._apply_cookies()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *_):
        await self.close()

    def _apply_cookies(self):
        for name, value in self.cookies.items():
            self.session.cookie_jar.update_cookies({name: value})

    def get_cookies_str(self) -> str:
        merged = dict(self.cookies)
        if self.session:
            for c in self.session.cookie_jar:
                if c.key and c.value:
                    merged[c.key] = c.value
        return json.dumps(merged)

    async def _request(self, method: str, url: str, read_bytes: bool = False, **kwargs):
        for attempt in range(2):
            async with getattr(self.session, method)(url, **kwargs) as resp:
                status = resp.status
                if read_bytes:
                    body_bytes = await resp.read()
                    body_text = body_bytes.decode("utf-8", errors="ignore")
                else:
                    body_text = await resp.text()
                    body_bytes = body_text.encode("utf-8")

                if is_cf_challenge(body_text, status):
                    if attempt == 0:
                        logger.warning(f"CF challenge detected at {url} — calling FlareSolverr")
                        result = await solve_challenge(IVASMS_BASE_URL)
                        if result:
                            self.cookies.update(result["cookies"])
                            self.user_agent = result["user_agent"]
                            await self.close()
                            await self.open()
                            continue
                    raise CFBlockedError(f"CF blocked after FlareSolverr attempt: {url}")

                resp._body_text = body_text
                resp._body_bytes = body_bytes
                return resp

    async def login(self) -> bool:
        try:
            resp = await self._request(
                "get",
                f"{IVASMS_BASE_URL}/portal/sms/received",
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=20),
            )
            if resp.status != 200:
                logger.error(f"login: HTTP {resp.status}")
                return False
            html = resp._body_text
            m = re.search(r'name="_token"\s+value="([^"]+)"', html)
            if m:
                self.csrf_token = m.group(1)
                logger.info("login OK — CSRF acquired")
                return True
            logger.error("login: CSRF not found — cookies may be expired")
            return False
        except CFBlockedError:
            logger.error("login: CF blocked even after FlareSolverr")
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
            for c in self.session.cookie_jar:
                if c.key and c.value:
                    self.cookies[c.key] = c.value
            m = re.search(r'name="_token"\s+value="([^"]+)"', resp._body_text)
            if m:
                self.csrf_token = m.group(1)
            logger.info("keepalive OK")
            return True
        except Exception as e:
            logger.error(f"keepalive: {e}")
            return False

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
            resp = await self._request("get", url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=30))
            data = json.loads(resp._body_text)
        except Exception as e:
            logger.error(f"get_wa_active_ranges: {e}")
            return []

        rows = data.get("data", [])
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
        payload = {"_token": self.csrf_token, "id": str(termination_id)}
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
                    data=payload, headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=15),
                )
                if resp.status == 429:
                    wait = 5 * (attempt + 1)
                    logger.warning(f"add_range 429 — waiting {wait}s (attempt {attempt+1})")
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
                data={"_token": self.csrf_token}, headers=hdrs,
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
