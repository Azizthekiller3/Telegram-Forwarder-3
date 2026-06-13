"""
/dualcopy — Dual-bot parallel copy handler.

Both Telethon userbots work simultaneously:
  • Bot 1 copies the first half of message IDs
  • Bot 2 copies the second half of message IDs
  • A shared asyncio.Lock + shared checkpoint keeps deduplication safe
  • Progress is reported in a single Telegram message, updated periodically
"""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

import userbot_bridge as bridge
from userbot.dual_forwarder import copy_channel_files_dual

logger = logging.getLogger(__name__)

_DUAL_TASK_KEY = "active_dual_copy_task"


# ─── /dualcopy ────────────────────────────────────────────────────────────────

async def dualcopy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /dualcopy <source_id> <dest_id> [options]

    Options (same as /copy):
      --restart   force restart (ignore checkpoint)
      --ext mkv,mp4   file extension filter
      --replace @old @new   caption username replacement
      --skiptext  skip text-only messages

    Examples:
      /dualcopy -1001234567890 -1009876543210
      /dualcopy -1001234567890 -1009876543210 --ext mkv,mp4 --restart
    """
    bot_data = context.bot_data

    # ── check both bots are ready ─────────────────────────────────────────────
    if not bridge.is_ready(bot_data):
        await update.message.reply_text(
            "❌ *Userbot 1 is not connected.*\n\n"
            "Use /login to connect your first account before running /dualcopy.",
            parse_mode="Markdown",
        )
        return

    if not bridge.is_ready2(bot_data):
        await update.message.reply_text(
            "❌ *Userbot 2 is not connected.*\n\n"
            "Use /login2 to connect your second account before running /dualcopy.",
            parse_mode="Markdown",
        )
        return

    # ── check no job is already running ───────────────────────────────────────
    existing = bot_data.get(_DUAL_TASK_KEY)
    if existing and not existing.done():
        await update.message.reply_text(
            "⚠️ A dual-copy job is already running.\n"
            "Use /stopdual to cancel it first.",
        )
        return

    existing_single = bot_data.get("active_copy_task")
    if existing_single and not existing_single.done():
        await update.message.reply_text(
            "⚠️ A single-bot copy job is already running.\n"
            "Use /stopjob to cancel it first.",
        )
        return

    # ── parse arguments ───────────────────────────────────────────────────────
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/dualcopy <source_id> <dest_id> [options]`\n\n"
            "Options:\n"
            "  `--restart`          — ignore checkpoint, start fresh\n"
            "  `--ext mkv,mp4`     — only copy these file types\n"
            "  `--replace @a @b`   — replace @a with @b in captions\n"
            "  `--skiptext`        — skip text-only messages\n\n"
            "Example:\n"
            "`/dualcopy -1001234567890 -1009876543210 --ext mkv,mp4`",
            parse_mode="Markdown",
        )
        return

    try:
        source_id = int(args[0])
        dest_id   = int(args[1])
    except ValueError:
        await update.message.reply_text(
            "❌ Source and destination must be numeric channel IDs.\n"
            "Example: `/dualcopy -1001234567890 -1009876543210`",
            parse_mode="Markdown",
        )
        return

    # Parse flags
    force_restart       = "--restart"  in args
    skip_text           = "--skiptext" in args
    allowed_exts        = None
    caption_replacement = ""

    if "--ext" in args:
        idx = args.index("--ext")
        if idx + 1 < len(args):
            from userbot.filter_utils import parse_ext_filter
            allowed_exts = parse_ext_filter(args[idx + 1])

    if "--replace" in args:
        idx = args.index("--replace")
        if idx + 2 < len(args):
            caption_replacement = args[idx + 2]

    # ── send the initial status message ───────────────────────────────────────
    client1 = bridge.get_client(bot_data)
    client2 = bridge.get_client2(bot_data)

    # Resolve names for display
    try:
        src_entity  = await client1.get_entity(source_id)
        src_name    = getattr(src_entity, "title", str(source_id))
    except Exception:
        src_name = str(source_id)

    try:
        dst_entity  = await client1.get_entity(dest_id)
        dst_name    = getattr(dst_entity, "title", str(dest_id))
    except Exception:
        dst_name = str(dest_id)

    ext_label = ", ".join(sorted(allowed_exts)).upper() if allowed_exts else "ALL"
    status_msg = await update.message.reply_text(
        f"🚀 *Dual-Bot Copy Starting…*\n\n"
        f"📡 Source: `{src_name}`\n"
        f"📥 Dest:   `{dst_name}`\n"
        f"🔎 Filter: `{ext_label}`\n"
        f"{'🔄 Force restart: yes' if force_restart else ''}\n\n"
        "⏳ Counting messages and splitting workload…",
        parse_mode="Markdown",
    )

    # ── launch dual-copy as a background task ─────────────────────────────────
    chat_id = update.effective_chat.id
    msg_id  = status_msg.message_id

    task = asyncio.create_task(
        _run_dual_copy(
            bot     = context.bot,
            chat_id = chat_id,
            msg_id  = msg_id,
            bot_data= bot_data,
            client1 = client1,
            client2 = client2,
            source_id           = source_id,
            dest_id             = dest_id,
            src_name            = src_name,
            dst_name            = dst_name,
            force_restart       = force_restart,
            allowed_exts        = allowed_exts,
            caption_replacement = caption_replacement,
            skip_text           = skip_text,
        )
    )
    bot_data[_DUAL_TASK_KEY] = task
    logger.info(f"Dual-copy task started: {source_id} → {dest_id}")


async def _run_dual_copy(
    bot, chat_id, msg_id, bot_data,
    client1, client2,
    source_id, dest_id, src_name, dst_name,
    force_restart, allowed_exts, caption_replacement, skip_text,
):
    """Run the dual forwarder and report results back to the bot message."""
    MIN_EDIT_INTERVAL = 8.0
    last_edit = [0.0]

    async def _progress(text: str, force: bool = False):
        now = time.time()
        if force or now - last_edit[0] >= MIN_EDIT_INTERVAL:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="Markdown",
                )
                last_edit[0] = now
            except Exception:
                pass

    try:
        await copy_channel_files_dual(
            client1             = client1,
            client2             = client2,
            source              = source_id,
            dest                = dest_id,
            force_restart       = force_restart,
            allowed_exts        = allowed_exts,
            caption_replacement = caption_replacement,
            skip_text           = skip_text,
            progress_cb         = _progress,
        )
    except asyncio.CancelledError:
        await _progress(
            f"⛔ *Dual-copy cancelled.*\n\n"
            f"📡 `{src_name}` → `{dst_name}`\n\n"
            "Progress was saved — use /dualcopy again to resume.",
            force=True,
        )
        return
    except Exception as e:
        logger.exception("Dual-copy error")
        await _progress(
            f"❌ *Dual-copy failed:* `{e}`\n\n"
            f"📡 `{src_name}` → `{dst_name}`\n\n"
            "Progress was saved — use /dualcopy again to resume.",
            force=True,
        )
        return
    finally:
        bot_data.pop(_DUAL_TASK_KEY, None)


# ─── /stopdual ────────────────────────────────────────────────────────────────

async def stopdual_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the running dual-copy job."""
    task = context.bot_data.get(_DUAL_TASK_KEY)
    if not task or task.done():
        await update.message.reply_text("ℹ️ No dual-copy job is currently running.")
        return

    task.cancel()
    await update.message.reply_text(
        "⛔ *Dual-copy job cancelled.*\n\n"
        "Progress was saved automatically — use /dualcopy again to resume.",
        parse_mode="Markdown",
    )
