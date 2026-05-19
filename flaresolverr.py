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
    "Chrome/124.0.0.0 Safari/537.36"
)

CUSTOM_UA = os.getenv(
    "CUSTOM_USER_AGENT",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36",
)


def is_cf_challenge(html: str, status: int) -> bool:
    """
    Detect a real Cloudflare challenge — NOT triggered by plain 403/expired session.
    Only returns True for genuine CF JS/Turnstile/CAPTCHA pages.
    """
    lower = html.lower()

    # 503 from Cloudflare is almost always a challenge
    if status == 503 and ("cloudflare" in lower or len(html) < 3000):
        return True

    # Strong CF challenge indicators
    cf_keywords = [
        "cf-browser-verification",
        "turnstile",
        "challenges.cloudflare.com",
        "performing security verification",
        "cf_chl_opt",
        "just a moment",
        "__cf_chl_",
        "cf_clearance",
        "enable javascript and cookies to continue",
    ]
    for kw in cf_keywords:
        if kw in lower:
            return True

    # Ray ID + Cloudflare = CF error/challenge page
    if "ray id" in lower and "cloudflare" in lower:
        return True

    # Very short page that mentions cloudflare (CF error page)
    if len(html) < 1500 and "cloudflare" in lower:
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
        ua = solution.get("userAgent", CUSTOM_UA)  # must match the UA used during CF solve  # always use our own UA instead of FlareSolverr's
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
            return ua if ua else CUSTOM_UA
    except FileNotFoundError:
        return CUSTOM_UA
