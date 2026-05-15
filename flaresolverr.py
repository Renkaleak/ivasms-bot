import asyncio
import json
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://localhost:8191/v1")
IVASMS_BASE_URL = os.getenv("IVASMS_BASE_URL", "https://www.ivasms.com")
UA_FILE = "ua.txt"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def is_cf_challenge(html: str, status: int) -> bool:
    if status in (403, 503):
        return True
    lower = html.lower()
    checks = [
        "cf-browser-verification",
        "turnstile",
        "challenges.cloudflare.com",
        "performing security verification",
        "cf_chl_opt",
    ]
    for c in checks:
        if c in lower:
            return True
    if "ray id" in lower and "cloudflare" in lower:
        return True
    if len(html) < 2000 and "cloudflare" in lower:
        return True
    return False


async def solve_challenge(url: str = IVASMS_BASE_URL) -> dict | None:
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                FLARESOLVERR_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                if resp.status != 200:
                    logger.error(f"FlareSolverr returned HTTP {resp.status}")
                    return None
                data = await resp.json(content_type=None)

        if data.get("status") != "ok":
            logger.error(f"FlareSolverr status not ok: {data.get('message')}")
            return None

        solution = data.get("solution", {})
        ua = solution.get("userAgent", DEFAULT_UA)
        raw_cookies = solution.get("cookies", [])

        cookies = {}
        for c in raw_cookies:
            name = c.get("name")
            value = c.get("value")
            if name and value:
                cookies[name] = value

        with open(UA_FILE, "w") as f:
            f.write(ua)

        logger.info(f"FlareSolverr solved OK — {len(cookies)} cookies, UA saved")
        return {"cookies": cookies, "user_agent": ua}

    except Exception as e:
        logger.error(f"solve_challenge error: {e}")
        return None


def get_saved_ua() -> str:
    try:
        with open(UA_FILE) as f:
            ua = f.read().strip()
            return ua if ua else DEFAULT_UA
    except FileNotFoundError:
        return DEFAULT_UA
