"""
In-bot Telethon userbot login wizard — Bot 1.

Two login methods are offered:
  A) OTP flow:  /login → phone number → OTP → (2FA) → done
  B) String session: /login → paste Telethon string session → done

Method B is useful when the account is already authenticated elsewhere
(e.g. another Telethon-based bot) and you want to skip the SMS step.
"""
from __future__ import annotations

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import userbot_bridge as bridge
from states import LOGIN_2FA, LOGIN_OTP, LOGIN_PHONE, LOGIN_STRING

logger = logging.getLogger(__name__)

# ── keyboards ─────────────────────────────────────────────────────────────────

_METHOD_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📱 Login with OTP",          callback_data="login_method_otp")],
    [InlineKeyboardButton("📋 Paste String Session",    callback_data="login_method_string")],
    [InlineKeyboardButton("❌ Cancel",                  callback_data="login_cancel")],
])
_RESEND_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔄 Resend code", callback_data="login_resend")],
    [InlineKeyboardButton("❌ Cancel",       callback_data="login_cancel")],
])
_CANCEL_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ Cancel", callback_data="login_cancel")],
])


def _menu_kb():
    from handlers.menu import main_menu_keyboard
    return main_menu_keyboard()


def _cleanup(context: ContextTypes.DEFAULT_TYPE):
    for k in ("login_phone", "login_sent", "login_otp_attempts", "login_resend_count"):
        context.user_data.pop(k, None)


def _where_was_code_sent(sent) -> str:
    try:
        type_name = type(sent.type).__name__
    except Exception:
        return "your Telegram"

    if "App" in type_name:
        return (
            "📱 *your Telegram app* (Saved Messages)\n"
            "👉 Open Telegram → tap *Saved Messages* → scroll to the *very bottom* — "
            "each request sends a new message, so the valid code is always the *last* one.\n"
            "_(Not SMS, not Service Notifications — it goes to Saved Messages because "
            "you're already logged into Telegram on this device.)_"
        )
    if "Sms" in type_name:
        return "📨 *SMS* to your phone number"
    if "FlashCall" in type_name:
        return "📞 *flash call* — the last digits of the caller's number are your code"
    if "MissedCall" in type_name:
        return "📞 *missed call* — the last digits of the caller's number are your code"
    if "Call" in type_name:
        return "📞 *automated phone call*"
    if "Email" in type_name:
        return "📧 *email*"
    return "your Telegram"


# ── entry ──────────────────────────────────────────────────────────────────────

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    async def _reply(text, **kw):
        if query:
            await query.edit_message_text(text, **kw)
        else:
            await update.message.reply_text(text, **kw)

    if bridge.is_ready(context.bot_data):
        await _reply(
            "✅ Userbot is already connected and ready.",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    client = bridge.get_client(context.bot_data)
    if client is None:
        api_id   = os.environ.get("TELEGRAM_API_ID",   "")
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        if not api_id or not api_hash:
            await _reply(
                "❌ Userbot client not initialised.\n"
                "Check that `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are set, "
                "then restart the bot.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
        else:
            await _reply(
                "⏳ *Userbot is still initialising…*\n\n"
                "The client is connecting in the background. "
                "Please wait a few seconds and try again.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
        return ConversationHandler.END

    await _reply(
        "🔑 *Userbot Login — Bot 1*\n\n"
        "Choose how to connect your Telegram account:\n\n"
        "• 📱 *OTP* — Telegram sends you a code (standard login)\n"
        "• 📋 *String Session* — paste a session exported from another Telethon app "
        "(skips the SMS step — handy if the account is already logged in elsewhere)\n\n"
        "Or just send your phone number to start the OTP flow directly.",
        parse_mode="Markdown",
        reply_markup=_METHOD_KB,
    )
    return LOGIN_PHONE


# ── method choice callbacks ────────────────────────────────────────────────────

async def login_choose_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped '📱 Login with OTP' — show the phone-number prompt."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📱 *OTP Login — Bot 1*\n\n"
        "Send your phone number with country code:\n"
        "Example: `+12345678901`",
        parse_mode="Markdown",
        reply_markup=_CANCEL_KB,
    )
    return LOGIN_PHONE


async def login_choose_string(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped '📋 Paste String Session' — show the paste prompt."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📋 *String Session Import — Bot 1*\n\n"
        "Paste your Telethon string session below.\n\n"
        "How to get it from another Telethon bot:\n"
        "```\nfrom telethon.sessions import StringSession\n"
        "print(StringSession.save(client.session))\n```\n\n"
        "The string is long (~400 chars) and starts with `1` or `BQIA…`\n\n"
        "_Your session will be verified before being saved._",
        parse_mode="Markdown",
        reply_markup=_CANCEL_KB,
    )
    return LOGIN_STRING


# ── phone number ───────────────────────────────────────────────────────────────

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    if not phone[1:].replace(" ", "").isdigit() or len(phone) < 8:
        await update.message.reply_text(
            "❌ That doesn't look like a valid phone number.\n"
            "Send it with country code, e.g. `+12345678901`",
            parse_mode="Markdown",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_PHONE

    client = bridge.get_client(context.bot_data)
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not send OTP: `{e}`\n\nCheck the number and try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    context.user_data["login_phone"]         = phone
    context.user_data["login_sent"]          = sent
    context.user_data["login_otp_attempts"]  = 0
    context.user_data["login_resend_count"]  = 0

    where = _where_was_code_sent(sent)
    await update.message.reply_text(
        f"✅ *Code sent to* {where}\n\n"
        "Enter the 5-digit OTP now.\n"
        "_Tap Resend if you don't receive it within 30 s._",
        parse_mode="Markdown",
        reply_markup=_RESEND_KB,
    )
    return LOGIN_OTP


# ── string session import ──────────────────────────────────────────────────────

async def login_string_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and import a pasted Telethon string session for Bot 1."""
    session_str = update.message.text.strip()

    # Basic sanity check — real Telethon strings are at least 100 chars
    if len(session_str) < 50:
        await update.message.reply_text(
            "❌ That doesn't look like a valid string session (too short).\n\n"
            "Paste the full string — it should be ~400 characters.\n"
            "Or tap /cancel to exit.",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_STRING

    status_msg = await update.message.reply_text(
        "⏳ *Verifying session string…*\n\n"
        "Connecting to Telegram to confirm it's valid…",
        parse_mode="Markdown",
    )

    try:
        me = await bridge.import_string_session(
            session_str, slot=1, bot_data=context.bot_data
        )
    except ValueError as e:
        await status_msg.edit_text(
            f"❌ *Import failed:*\n\n{e}\n\n"
            "Check the session string and try again, or use /login for OTP login.",
            parse_mode="Markdown",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_STRING
    except Exception as e:
        logger.exception("login string session import error (Bot 1)")
        await status_msg.edit_text(
            f"❌ *Unexpected error:* `{e}`\n\nUse /login to try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    name  = me.first_name or ""
    uname = f"@{me.username}" if me.username else f"id={me.id}"
    _cleanup(context)

    from handlers.menu import main_menu_keyboard
    await status_msg.edit_text(
        f"✅ *Bot 1 session imported — logged in as {name} ({uname})!*\n\n"
        "The userbot is reconnecting in the background (takes ~5 s).\n"
        "All copy/sync features will be active shortly.\n\n"
        "Use /menu to get started.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(userbot_ready=False),
    )
    return ConversationHandler.END


# ── OTP ────────────────────────────────────────────────────────────────────────

async def _do_resend(phone: str, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, int]:
    client = bridge.get_client(context.bot_data)
    try:
        from telethon.errors import FloodWaitError
        sent = await client.send_code_request(phone)
        context.user_data["login_sent"]         = sent
        context.user_data["login_otp_attempts"] = 0
        return True, 0
    except FloodWaitError as fw:
        logger.warning(f"Resend flood-wait: {fw.seconds}s")
        return False, fw.seconds
    except Exception as e:
        logger.warning(f"Resend failed: {e}")
        return False, 0


async def login_resend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Sending new code…")

    phone = context.user_data.get("login_phone")
    ok, flood_secs = await _do_resend(phone, context)
    if not ok:
        if flood_secs:
            mins = flood_secs // 60
            wait_msg = f"{mins}m {flood_secs % 60}s" if mins else f"{flood_secs}s"
            await query.edit_message_text(
                f"⏳ *Telegram is rate-limiting code requests.*\n\n"
                f"Please wait *{wait_msg}* then tap /login to try again.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
        else:
            await query.edit_message_text(
                "❌ Could not resend the code. Use /login to start over.",
                reply_markup=_menu_kb(),
            )
        return ConversationHandler.END

    sent  = context.user_data.get("login_sent")
    where = _where_was_code_sent(sent) if sent else "your Telegram"
    await query.edit_message_text(
        f"✅ *New code sent to* {where}\n\n"
        "Enter the 5-digit OTP now.",
        parse_mode="Markdown",
        reply_markup=_RESEND_KB,
    )
    return LOGIN_OTP


async def login_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code   = update.message.text.strip().replace(" ", "")
    phone  = context.user_data.get("login_phone")
    sent   = context.user_data.get("login_sent")
    client = bridge.get_client(context.bot_data)

    try:
        from telethon.errors import (
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)

    except PhoneCodeExpiredError:
        resend_count = context.user_data.get("login_resend_count", 0) + 1
        context.user_data["login_resend_count"] = resend_count

        if resend_count > 2:
            _cleanup(context)
            await update.message.reply_text(
                "⚠️ *This keeps happening because you may be entering an old code.*\n\n"
                "Each login request sends a *new* code to Saved Messages.\n"
                "👉 Open Telegram → *Saved Messages* → scroll to the *very bottom* — "
                "use only the *last* code in the chat.\n\n"
                "Use /login to start fresh with a new code.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
            return ConversationHandler.END

        ok, flood_secs = await _do_resend(phone, context)
        if not ok:
            if flood_secs:
                mins = flood_secs // 60
                wait_msg = f"{mins}m {flood_secs % 60}s" if mins else f"{flood_secs}s"
                await update.message.reply_text(
                    f"⏳ *Telegram is rate-limiting code requests.*\n\n"
                    f"Please wait *{wait_msg}* then use /login to try again.",
                    parse_mode="Markdown",
                    reply_markup=_menu_kb(),
                )
            else:
                await update.message.reply_text(
                    "❌ Could not auto-resend the code. Use /login to start over.",
                    reply_markup=_menu_kb(),
                )
            return ConversationHandler.END

        sent  = context.user_data.get("login_sent")
        where = _where_was_code_sent(sent) if sent else "your Telegram"
        await update.message.reply_text(
            f"⚠️ *That code expired — a fresh one has been sent.*\n\n"
            f"Sent to {where}\n\n"
            "Enter the new code below.\n"
            "_Still not working? Use /login to start fresh._",
            parse_mode="Markdown",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_OTP

    except PhoneCodeInvalidError:
        attempts = context.user_data.get("login_otp_attempts", 0) + 1
        context.user_data["login_otp_attempts"] = attempts
        if attempts >= 3:
            _cleanup(context)
            await update.message.reply_text(
                "❌ Too many incorrect codes. Use /login to start over.",
                reply_markup=_menu_kb(),
            )
            return ConversationHandler.END
        left = 3 - attempts
        await update.message.reply_text(
            f"❌ Wrong code ({left} attempt{'s' if left != 1 else ''} left). Try again:",
            reply_markup=_RESEND_KB,
        )
        return LOGIN_OTP

    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 *Two-step verification is enabled.*\n\n"
            "Send your 2FA cloud password:",
            parse_mode="Markdown",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_2FA

    except Exception as e:
        logger.exception("sign_in error")
        _cleanup(context)
        await update.message.reply_text(
            f"❌ Sign-in error: `{e}`\n\nUse /login to try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    return await _login_success(update, context)


# ── 2FA password ───────────────────────────────────────────────────────────────

async def login_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client   = bridge.get_client(context.bot_data)

    try:
        from telethon.errors import PasswordHashInvalidError
        await client.sign_in(password=password)

    except PasswordHashInvalidError:
        await update.message.reply_text(
            "❌ Wrong password. Try again:",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_2FA

    except Exception as e:
        logger.exception("2FA error")
        _cleanup(context)
        await update.message.reply_text(
            f"❌ 2FA error: `{e}`\n\nUse /login to try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    return await _login_success(update, context)


# ── cancel ─────────────────────────────────────────────────────────────────────

async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cleanup(context)
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "❌ Login cancelled.",
            reply_markup=_menu_kb(),
        )
    else:
        await update.message.reply_text(
            "❌ Login cancelled.",
            reply_markup=_menu_kb(),
        )
    return ConversationHandler.END


# ── success ────────────────────────────────────────────────────────────────────

async def _login_success(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from handlers.menu import main_menu_keyboard
    client = bridge.get_client(context.bot_data)
    me     = await client.get_me()
    name   = me.first_name or ""
    uname  = f"@{me.username}" if me.username else f"id={me.id}"

    context.bot_data["userbot_ready"] = True
    _cleanup(context)

    await update.message.reply_text(
        f"✅ *Logged in as {name} ({uname})!*\n\n"
        "All userbot features (copy, sync, history) are now active.\n\n"
        "Use /menu to get started.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(userbot_ready=True),
    )
    return ConversationHandler.END


# ── ConversationHandler builder ────────────────────────────────────────────────

def build_login_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("login", login_start),
            CallbackQueryHandler(login_start, pattern="^userbot_login$"),
        ],
        allow_reentry=True,
        states={
            LOGIN_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone),
                CallbackQueryHandler(login_choose_otp,    pattern="^login_method_otp$"),
                CallbackQueryHandler(login_choose_string, pattern="^login_method_string$"),
                CallbackQueryHandler(login_cancel,        pattern="^login_cancel$"),
            ],
            LOGIN_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp),
                CallbackQueryHandler(login_resend, pattern="^login_resend$"),
                CallbackQueryHandler(login_cancel, pattern="^login_cancel$"),
            ],
            LOGIN_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_2fa),
                CallbackQueryHandler(login_cancel, pattern="^login_cancel$"),
            ],
            LOGIN_STRING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_string_session),
                CallbackQueryHandler(login_cancel, pattern="^login_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", login_cancel),
        ],
        per_chat=False,
        per_user=True,
        per_message=False,
    )
