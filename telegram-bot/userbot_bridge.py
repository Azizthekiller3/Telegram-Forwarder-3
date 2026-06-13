"""
Manages shared Telethon userbot clients (Bot 1 and Bot 2) inside PTB's asyncio event loop.

Bot 1: sessions/userbot   — primary account, used for /login
Bot 2: sessions/userbot2  — second account, used for /login2 and dual-copy
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

API_ID   = int(os.environ.get("TELEGRAM_API_ID",   "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

SESSION_PATH  = os.path.join(os.path.dirname(__file__), "sessions", "userbot")
SESSION2_PATH = os.path.join(os.path.dirname(__file__), "sessions", "userbot2")

_FAST_RETRIES = 12
_FAST_DELAY   = 5
_SLOW_DELAY   = 30


async def _connect_loop(bot_data: dict, slot: int = 1) -> None:
    """
    Background task — connects a Telethon client and keeps it available.
    slot=1 → primary userbot (bot_data keys: userbot_client, userbot_ready, …)
    slot=2 → second userbot (bot_data keys: userbot2_client, userbot2_ready, …)
    """
    key_client  = "userbot_client"  if slot == 1 else "userbot2_client"
    key_ready   = "userbot_ready"   if slot == 1 else "userbot2_ready"
    key_locked  = "userbot_locked"  if slot == 1 else "userbot2_locked"
    session     = SESSION_PATH      if slot == 1 else SESSION2_PATH
    label       = "Userbot"         if slot == 1 else "Userbot2"

    try:
        from telethon import TelegramClient
    except ImportError:
        logger.warning("telethon not installed — userbot commands disabled")
        return

    os.makedirs(os.path.join(os.path.dirname(__file__), "sessions"), exist_ok=True)

    attempt = 0
    while True:
        attempt += 1
        client = None
        try:
            client = TelegramClient(session, API_ID, API_HASH)
            await client.connect()

            bot_data[key_client] = client
            bot_data.pop(key_locked, None)

            if not await client.is_user_authorized():
                logger.warning(f"{label} session not authorised — use /login{'2' if slot==2 else ''} to sign in.")
                bot_data[key_ready] = False

                authorised = False
                while True:
                    await asyncio.sleep(10)
                    try:
                        if await client.is_user_authorized():
                            authorised = True
                            break
                    except Exception:
                        logger.warning(f"{label} auth-poll error — reconnecting")
                        break

                if not authorised:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    bot_data[key_ready] = False
                    await asyncio.sleep(_FAST_DELAY)
                    continue

            me = await client.get_me()
            bot_data[key_client] = client
            bot_data[key_ready]  = True
            logger.info(f"{label} bridge connected as {me.first_name} (@{me.username})")

            while True:
                await asyncio.sleep(30)
                try:
                    if not client.is_connected():
                        logger.warning(f"{label} connection lost — reconnecting…")
                        break
                    if not await client.is_user_authorized():
                        logger.warning(f"{label} session deauthorised — reconnecting…")
                        break
                except Exception as e:
                    logger.warning(f"{label} health-check failed: {e} — reconnecting…")
                    break

            bot_data[key_ready] = False
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(_FAST_DELAY)
            continue

        except Exception as e:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            if "database is locked" in str(e).lower():
                if attempt == 1:
                    logger.warning(f"{label} session locked — will retry automatically.")
                    bot_data[key_locked] = True
                elif attempt == _FAST_RETRIES + 1:
                    logger.warning(f"{label} still locked — switching to slow retries (30 s).")
                delay = _FAST_DELAY if attempt <= _FAST_RETRIES else _SLOW_DELAY
                await asyncio.sleep(delay)
            else:
                logger.error(f"{label} connect failed: {e}")
                await asyncio.sleep(_SLOW_DELAY)


async def init_userbot(application) -> None:
    """PTB post_init hook — starts both connection tasks and returns immediately."""
    bot_data = application.bot_data
    bot_data.setdefault("active_copy_task",    None)
    bot_data.setdefault("active_sync_task",    None)
    bot_data.setdefault("active_sync_handler", None)
    bot_data.setdefault("active_copy_stats",   {})
    bot_data.setdefault("userbot_ready",       False)
    bot_data.setdefault("userbot2_ready",      False)

    if not API_ID or not API_HASH:
        logger.warning("TELEGRAM_API_ID/HASH not set — userbot commands disabled")
        return

    task1 = asyncio.create_task(_connect_loop(bot_data, slot=1))
    bot_data["_userbot_connect_task"] = task1

    task2 = asyncio.create_task(_connect_loop(bot_data, slot=2))
    bot_data["_userbot2_connect_task"] = task2

    logger.info("Userbot bridge tasks started in background (Bot1 + Bot2)")


# ── Bot 1 helpers ─────────────────────────────────────────────────────────────

def get_client(bot_data: dict):
    return bot_data.get("userbot_client")

def is_ready(bot_data: dict) -> bool:
    return bot_data.get("userbot_ready", False)

def is_locked(bot_data: dict) -> bool:
    return bot_data.get("userbot_locked", False)


# ── Bot 2 helpers ─────────────────────────────────────────────────────────────

def get_client2(bot_data: dict):
    return bot_data.get("userbot2_client")

def is_ready2(bot_data: dict) -> bool:
    return bot_data.get("userbot2_ready", False)

def is_locked2(bot_data: dict) -> bool:
    return bot_data.get("userbot2_locked", False)

def both_ready(bot_data: dict) -> bool:
    return is_ready(bot_data) and is_ready2(bot_data)
