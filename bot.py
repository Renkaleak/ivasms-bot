import asyncio
import json
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from ivasms import IVASMSClient, xlsx_bytes_to_numbers, numbers_to_txt, CFBlockedError

logger = logging.getLogger(__name__)

_monitor_tasks: dict[int, asyncio.Task] = {}
_scan_cache: dict[int, dict] = {}


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📡 Scan WA Range"), KeyboardButton(text="📦 My Numbers")],
            [KeyboardButton(text="🔄 Return & Refresh"), KeyboardButton(text="📡 Live Monitor")],
            [KeyboardButton(text="📋 OTP History"),      KeyboardButton(text="ℹ️ Status")],
            [KeyboardButton(text="📥 Export Numbers"),   KeyboardButton(text="🍪 Set Cookies")],
        ],
        resize_keyboard=True,
    )


def admin_only(handler):
    async def wrapper(msg: Message, **kwargs):
        if not db.is_admin(msg.from_user.id):
            return
        return await handler(msg)
    return wrapper


def register_handlers(dp: Dispatcher, bot: Bot):

    @dp.message(Command("start"))
    @admin_only
    async def cmd_start(msg: Message):
        await msg.answer(
            "👋 <b>iVAS SMS Bot</b>\n\nSelect an option below:",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )

    @dp.message(F.text == "🍪 Set Cookies")
    @admin_only
    async def prompt_set_cookies(msg: Message):
        await msg.answer(
            "📋 Paste your ivasms.com cookies as JSON array:\n\n"
            "<code>[{\"name\":\"XSRF-TOKEN\",\"value\":\"...\"},{\"name\":\"ivas_sms_session\",\"value\":\"...\"}]</code>\n\n"
            "Get them from: F12 → Application → Cookies → ivasms.com",
            parse_mode="HTML",
        )

    @dp.message(F.text.startswith("["))
    @admin_only
    async def handle_cookies_input(msg: Message):
        uid = msg.from_user.id
        raw = msg.text.strip()
        wait = await msg.answer("🔄 Validating cookies...")
        async with IVASMSClient(raw) as client:
            ok = await client.login()
        if ok:
            db.set_user_cookies(uid, raw)
            await wait.edit_text("✅ <b>Cookies saved!</b> CSRF token acquired. Session is active.", parse_mode="HTML")
        else:
            await wait.edit_text(
                "❌ <b>Invalid cookies</b> or Cloudflare is blocking.\n"
                "Try /refreshcf to solve the challenge first.",
                parse_mode="HTML",
            )

    @dp.message(Command("refreshcf"))
    @admin_only
    async def cmd_refresh_cf(msg: Message):
        wait = await msg.answer("🔄 Calling FlareSolverr...")
        from flaresolverr import solve_challenge
        result = await solve_challenge()
        if result:
            import json as _json
            new_cookies = _json.dumps(result["cookies"])
            db.set_user_cookies(msg.from_user.id, new_cookies, result["user_agent"])
            await wait.edit_text("✅ <b>CF solved!</b> Cookies updated from FlareSolverr.", parse_mode="HTML")
        else:
            await wait.edit_text("❌ FlareSolverr failed. Is it running?", parse_mode="HTML")

    @dp.message(F.text == "ℹ️ Status")
    @admin_only
    async def show_status(msg: Message):
        uid = msg.from_user.id
        cookies = db.get_cookies(uid)
        monitoring = uid in _monitor_tasks and not _monitor_tasks[uid].done()

        if cookies:
            async with IVASMSClient(cookies) as client:
                ok = await client.login()
                count = await client.get_my_numbers_count() if ok else -1
            cookie_status = "✅ Valid" if ok else "❌ Expired / Blocked"
        else:
            cookie_status = "⚠️ Not set"
            count = -1

        from flaresolverr import FLARESOLVERR_URL
        await msg.answer(
            f"<b>Bot Status</b>\n\n"
            f"🍪 Cookies: {cookie_status}\n"
            f"📦 My Numbers: {count if count >= 0 else 'N/A'}\n"
            f"📡 Live Monitor: {'🟢 Active' if monitoring else '🔴 Off'}\n"
            f"🔧 FlareSolverr: <code>{FLARESOLVERR_URL}</code>\n",
            parse_mode="HTML",
        )

    @dp.message(F.text == "📡 Scan WA Range")
    @admin_only
    async def scan_wa_range(msg: Message):
        uid = msg.from_user.id
        cookies = db.get_cookies(uid)
        if not cookies:
            await msg.answer("⚠️ Set your cookies first via 🍪 Set Cookies")
            return
        wait = await msg.answer("🔄 Scanning WhatsApp-active ranges...")
        async with IVASMSClient(cookies) as client:
            await client.login()
            ranges = await client.get_wa_active_ranges()

        if not ranges:
            await wait.edit_text("❌ No WA-active ranges found or session expired.")
            return

        by_country: dict[str, list] = {}
        for r in ranges:
            by_country.setdefault(r["country"], []).append(r)

        countries = sorted(by_country.keys())
        buttons = []
        for i, country in enumerate(countries):
            total = sum(r["count"] for r in by_country[country])
            buttons.append([InlineKeyboardButton(
                text=f"{country} ({total} SMS)",
                callback_data=f"wa_country:{i}",
            )])

        _scan_cache[uid] = {"by_country": by_country, "countries": countries}

        await wait.edit_text(
            f"📡 <b>WA-Active Ranges</b>\n\nFound <b>{len(ranges)}</b> ranges across <b>{len(countries)}</b> countries.\nSelect a country:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )

    @dp.callback_query(F.data.startswith("wa_country:"))
    async def cb_wa_country(cb: CallbackQuery):
        uid = cb.from_user.id
        idx = int(cb.data.split(":")[1])
        cache = _scan_cache.get(uid)
        if not cache:
            await cb.answer("Session expired, scan again")
            return
        country = cache["countries"][idx]
        ranges = cache["by_country"][country]

        buttons = []
        for r in ranges[:20]:
            buttons.append([InlineKeyboardButton(
                text=f"➕ {r['range']} ({r['count']} SMS)",
                callback_data=f"add_range:{r['termination_id']}",
            )])
        buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data="wa_back")])

        await cb.message.edit_text(
            f"📡 <b>{country}</b> — {len(ranges)} range(s)\nTap to add:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )
        await cb.answer()

    @dp.callback_query(F.data.startswith("add_range:"))
    async def cb_add_range(cb: CallbackQuery):
        uid = cb.from_user.id
        tid = cb.data.split(":")[1]
        cookies = db.get_cookies(uid)
        await cb.answer("Adding range...")
        async with IVASMSClient(cookies) as client:
            await client.login()
            result = await client.add_range(tid)
        status = "✅" if result["ok"] else "❌"
        await cb.message.answer(f"{status} {result['message']}", parse_mode="HTML")

    @dp.callback_query(F.data == "wa_back")
    async def cb_wa_back(cb: CallbackQuery):
        await scan_wa_range(cb.message)
        await cb.answer()

    @dp.message(F.text == "📦 My Numbers")
    @admin_only
    async def show_my_numbers(msg: Message):
        uid = msg.from_user.id
        cookies = db.get_cookies(uid)
        if not cookies:
            await msg.answer("⚠️ Set cookies first.")
            return
        wait = await msg.answer("🔄 Fetching...")
        async with IVASMSClient(cookies) as client:
            await client.login()
            count = await client.get_my_numbers_count()
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Download TXT", callback_data="dl_txt")],
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="my_numbers_refresh")],
        ])
        await wait.edit_text(
            f"📦 <b>My Numbers</b>\n\nTotal: <b>{count}</b> number(s)",
            reply_markup=buttons,
            parse_mode="HTML",
        )

    @dp.callback_query(F.data == "my_numbers_refresh")
    async def cb_my_numbers_refresh(cb: CallbackQuery):
        await show_my_numbers(cb.message)
        await cb.answer()

    @dp.callback_query(F.data == "dl_txt")
    async def cb_dl_txt(cb: CallbackQuery):
        uid = cb.from_user.id
        cookies = db.get_cookies(uid)
        await cb.answer("Downloading...")
        async with IVASMSClient(cookies) as client:
            await client.login()
            data = await client.download_xlsx()
        if not data:
            await cb.message.answer("❌ Failed to download XLSX.")
            return
        numbers = xlsx_bytes_to_numbers(data)
        txt = numbers_to_txt(numbers)
        from aiogram.types import BufferedInputFile
        await cb.message.answer_document(
            BufferedInputFile(txt, filename="numbers.txt"),
            caption=f"📥 {len(numbers)} numbers exported",
        )

    @dp.message(F.text == "📥 Export Numbers")
    @admin_only
    async def export_numbers(msg: Message):
        uid = msg.from_user.id
        cookies = db.get_cookies(uid)
        if not cookies:
            await msg.answer("⚠️ Set cookies first.")
            return
        wait = await msg.answer("🔄 Downloading XLSX...")
        async with IVASMSClient(cookies) as client:
            await client.login()
            data = await client.download_xlsx()
        if not data:
            await wait.edit_text("❌ Download failed.")
            return
        numbers = xlsx_bytes_to_numbers(data)
        txt = numbers_to_txt(numbers)
        from aiogram.types import BufferedInputFile
        await wait.delete()
        await msg.answer_document(
            BufferedInputFile(txt, filename="numbers.txt"),
            caption=f"📥 <b>{len(numbers)}</b> numbers exported to TXT",
            parse_mode="HTML",
        )

    @dp.message(F.text == "🔄 Return & Refresh")
    @admin_only
    async def return_refresh(msg: Message):
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yes, return all", callback_data="confirm_return")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_return")],
        ])
        await msg.answer("⚠️ <b>Return ALL numbers?</b>\nThis cannot be undone.", reply_markup=buttons, parse_mode="HTML")

    @dp.callback_query(F.data == "confirm_return")
    async def cb_confirm_return(cb: CallbackQuery):
        uid = cb.from_user.id
        cookies = db.get_cookies(uid)
        await cb.answer("Processing...")
        await cb.message.edit_text("🔄 Returning all numbers...")
        async with IVASMSClient(cookies) as client:
            await client.login()
            result = await client.bulk_return_all()
        status = "✅" if result["ok"] else "❌"
        await cb.message.edit_text(
            f"{status} <b>Return complete</b>\nReturned: <b>{result['count']}</b> numbers\n{result['message']}",
            parse_mode="HTML",
        )

    @dp.callback_query(F.data == "cancel_return")
    async def cb_cancel_return(cb: CallbackQuery):
        await cb.message.edit_text("❌ Cancelled.")
        await cb.answer()

    @dp.message(F.text == "📡 Live Monitor")
    @admin_only
    async def toggle_monitor(msg: Message):
        uid = msg.from_user.id
        if uid in _monitor_tasks and not _monitor_tasks[uid].done():
            _monitor_tasks[uid].cancel()
            db.set_monitoring(uid, False)
            await msg.answer("🔴 <b>Live Monitor stopped.</b>", parse_mode="HTML")
        else:
            cookies = db.get_cookies(uid)
            if not cookies:
                await msg.answer("⚠️ Set cookies first.")
                return
            task = asyncio.create_task(_monitor_loop(bot, msg.chat.id, uid, cookies))
            _monitor_tasks[uid] = task
            db.set_monitoring(uid, True)
            await msg.answer("🟢 <b>Live Monitor starting...</b>", parse_mode="HTML")

    @dp.message(F.text == "📋 OTP History")
    @admin_only
    async def otp_history(msg: Message):
        uid = msg.from_user.id
        cookies = db.get_cookies(uid)
        if not cookies:
            await msg.answer("⚠️ Set cookies first.")
            return
        wait = await msg.answer("🔄 Fetching today's SMS...")
        async with IVASMSClient(cookies) as client:
            await client.login()
            sms_list = await client.get_received_sms_today()
        if not sms_list:
            await wait.edit_text("📭 No SMS received today.")
            return
        lines = []
        for s in sms_list[-10:]:
            lines.append(
                f"📞 <code>{s['number']}</code>\n"
                f"👤 {s['originator']}\n"
                f"💬 {s['message']}\n"
                f"🕐 {s['time']}"
            )
        await wait.edit_text(
            f"📋 <b>OTP History (today, last {len(lines)})</b>\n\n" + "\n\n".join(lines),
            parse_mode="HTML",
        )


async def _monitor_loop(bot: Bot, chat_id: int, uid: int, cookies_raw: str):
    import socketio as sio_lib

    reconnect_delay = 5

    while True:
        try:
            async with IVASMSClient(cookies_raw) as client:
                ok = await client.login()
                if not ok:
                    await bot.send_message(chat_id, "❌ Session expired. Update cookies via 🍪 Set Cookies")
                    return

                params = await client.get_live_sms_socket_params()
                if not params:
                    await bot.send_message(chat_id, "❌ Could not get socket params from /portal/live/my_sms")
                    return

                token = params["token"]
                event_name = params["event_name"]
                cookie_str = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
                conn_url = f"https://ivasms.com:2087/livesms?token={token}"

                sio = sio_lib.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
                connected_ev = asyncio.Event()
                disconnected_ev = asyncio.Event()

                @sio.event(namespace="/livesms")
                async def connect():
                    connected_ev.set()
                    await bot.send_message(
                        chat_id,
                        "✅ <b>Live Monitor connected</b>\nWaiting for incoming SMS...\n<i>Keepalive runs every 20 min.</i>",
                        parse_mode="HTML",
                    )

                @sio.event(namespace="/livesms")
                async def disconnect():
                    disconnected_ev.set()

                @sio.on(event_name, namespace="/livesms")
                async def on_sms(data):
                    try:
                        number = data.get("number", "?")
                        originator = data.get("originator", data.get("sender", "?"))
                        message = data.get("messagedata", data.get("message", "?"))
                        time_str = data.get("senttime", data.get("time", ""))
                        await bot.send_message(
                            chat_id,
                            f"📨 <b>Incoming SMS</b>\n"
                            f"📞 <code>{number}</code>\n"
                            f"👤 From: <code>{originator}</code>\n"
                            f"💬 {message}\n"
                            f"🕐 {time_str}",
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.error(f"on_sms handler: {e}")

                await sio.connect(
                    conn_url,
                    transports=["websocket"],
                    headers={"Cookie": cookie_str},
                    wait_timeout=15,
                )

                try:
                    await asyncio.wait_for(connected_ev.wait(), timeout=20)
                except asyncio.TimeoutError:
                    await sio.disconnect()
                    raise Exception("Socket timeout — ivasms.com did not respond in 20s")

                async def _keepalive():
                    while not disconnected_ev.is_set():
                        await asyncio.sleep(1200)
                        alive = await client.keepalive()
                        if not alive:
                            await bot.send_message(chat_id, "⚠️ <b>Session expired during monitor.</b> Update cookies.", parse_mode="HTML")
                            disconnected_ev.set()
                            break

                ka_task = asyncio.create_task(_keepalive())
                await disconnected_ev.wait()
                ka_task.cancel()
                await sio.disconnect()
                raise Exception("Socket disconnected — reconnecting")

        except asyncio.CancelledError:
            logger.info(f"Monitor task for uid {uid} cancelled")
            break
        except Exception as e:
            logger.warning(f"Monitor loop uid={uid}: {e} — retrying in {reconnect_delay}s")
            await asyncio.sleep(reconnect_delay)
