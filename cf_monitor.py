import asyncio
import logging

import aiohttp

from flaresolverr import is_cf_challenge, solve_challenge, DEFAULT_UA, IVASMS_BASE_URL

logger = logging.getLogger(__name__)


class CFMonitor:
    """
    Probes ivasms.com every 60 seconds.
    If a CF challenge is detected:
      - Sends alert to all admin Telegram chats
      - Calls FlareSolverr to solve it
      - Updates cookies for all active users via the provided callback
      - If still blocked, retries every 2 minutes until resolved
    """

    def __init__(
        self,
        bot,
        get_all_users_fn,
        update_cookies_fn,
        alert_chat_ids: list[int],
        check_interval: int = 60,
        retry_interval: int = 120,
    ):
        self.bot = bot
        self.get_all_users = get_all_users_fn
        self.update_cookies = update_cookies_fn
        self.alert_chat_ids = alert_chat_ids
        self.check_interval = check_interval
        self.retry_interval = retry_interval
        self.is_blocked = False

    async def _probe(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{IVASMS_BASE_URL}/portal/dashboard",
                    headers={"User-Agent": DEFAULT_UA},
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    html = await resp.text()
                    return is_cf_challenge(html, resp.status)
        except Exception as e:
            logger.error(f"CFMonitor probe error: {e}")
            return False

    async def _alert(self, text: str):
        for chat_id in self.alert_chat_ids:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"CFMonitor alert send error: {e}")

    async def _solve_and_update(self) -> bool:
        result = await solve_challenge(IVASMS_BASE_URL)
        if not result:
            await self._alert("❌ <b>FlareSolverr failed to solve CF challenge.</b>\nWill retry in 2 minutes.")
            return False

        users = self.get_all_users()
        import json
        new_cookies_str = json.dumps(result["cookies"])
        new_ua = result["user_agent"]

        updated = 0
        for uid in users:
            try:
                self.update_cookies(uid, new_cookies_str, new_ua)
                updated += 1
            except Exception as e:
                logger.error(f"CFMonitor update_cookies for uid {uid}: {e}")

        await self._alert(
            f"✅ <b>CF Challenge Solved</b>\n"
            f"Cookies updated for <b>{updated}</b> active user(s).\n"
            f"Bot is back online."
        )
        return True

    async def run(self):
        logger.info("CFMonitor started")
        while True:
            try:
                detected = await self._probe()

                if detected and not self.is_blocked:
                    self.is_blocked = True
                    await self._alert(
                        "⚠️ <b>Cloudflare Challenge Detected!</b>\n"
                        "ivasms.com is showing a Turnstile/CF challenge.\n"
                        "Attempting to solve via FlareSolverr..."
                    )
                    solved = await self._solve_and_update()
                    if solved:
                        self.is_blocked = False
                    else:
                        await asyncio.sleep(self.retry_interval)
                        continue

                elif detected and self.is_blocked:
                    logger.warning("CFMonitor: still blocked, retrying solve")
                    solved = await self._solve_and_update()
                    if solved:
                        self.is_blocked = False
                    else:
                        await asyncio.sleep(self.retry_interval)
                        continue

                elif not detected and self.is_blocked:
                    self.is_blocked = False
                    await self._alert("✅ <b>CF Challenge cleared.</b> ivasms.com is accessible again.")

                await asyncio.sleep(self.check_interval)

            except asyncio.CancelledError:
                logger.info("CFMonitor cancelled")
                break
            except Exception as e:
                logger.error(f"CFMonitor loop error: {e}")
                await asyncio.sleep(30)
