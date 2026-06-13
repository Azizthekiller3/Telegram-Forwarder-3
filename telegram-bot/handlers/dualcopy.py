"""
/dualcopy  — Dual-bot parallel copy handler.
/stopdual  — Cancel the running dual-copy job.
/status2   — Per-bot live progress breakdown (Bot 1 vs Bot 2 side-by-side).

Both Telethon userbots work simultaneously:
  • Bot 1 copies the first half of message IDs
  • Bot 2 copies the second half of message IDs
  • A shared asyncio.Lock + shared checkpoint keeps deduplication safe
  • Progress is reported in a single Telegram message, updated periodically
  • /status2 reads per-bot stats stored in bot_data for a side-by-side view
"""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

import userbot_bridge as bridge
from userbot.dual_forwarder import copy_channel_files_dual, DUAL_STATUS_KEY

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
            bot_data            = bot_data,
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


# ─── /status2 ─────────────────────────────────────────────────────────────────

def _rate_str(copied: int, elapsed: float) -> str:
    """Return a human-readable copy rate string."""
    if elapsed < 1 or copied == 0:
        return "—"
    rate = copied / elapsed
    if rate >= 1:
        return f"{rate:.1f} msg/s"
    return f"{rate * 60:.1f} msg/min"


def _eta_str(remaining: int, copied: int, elapsed: float) -> str:
    if copied == 0 or elapsed < 1:
        return "?"
    rate = copied / elapsed
    secs = int(remaining / rate)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _mini_bar(done: int, total: int, width: int = 10) -> str:
    pct = min(100, int(done / max(total, 1) * 100))
    filled = pct * width // 100
    return "█" * filled + "░" * (width - filled)


async def status2_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status2 — Show live per-bot progress for the running dual-copy job.

    Displays Bot 1 and Bot 2 side-by-side with:
      • Individual copy counts and rates
      • Each bot's progress through its assigned range
      • ETA per bot and combined ETA
    """
    status = context.bot_data.get(DUAL_STATUS_KEY)

    if not status:
        task = context.bot_data.get(_DUAL_TASK_KEY)
        if task and not task.done():
            await update.message.reply_text(
                "⚡ *Dual-copy is starting up…*\n\n"
                "Stats will be available in a moment. Try again in a few seconds.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "ℹ️ No dual-copy job is currently running.\n\n"
                "Use /dualcopy to start one.",
            )
        return

    now        = time.time()
    src        = status["source_name"]
    dst        = status["dest_name"]
    total      = status["total"]
    started_at = status["started_at"]
    elapsed    = now - started_at
    shared     = status["shared"]
    b1         = status["bot1"]
    b2         = status["bot2"]

    # ── per-bot metrics ───────────────────────────────────────────────────────
    def _bot_block(label: str, s: dict) -> str:
        assigned  = s["total_assigned"]
        copied    = s["copied"]
        skipped   = s["skipped"]
        failed    = s["failed"]
        done      = s.get("done", False)
        processed = copied + skipped + failed
        remaining = max(0, assigned - processed)

        bar       = _mini_bar(processed, assigned)
        pct       = min(100, int(processed / max(assigned, 1) * 100))
        rate      = _rate_str(copied, elapsed)
        eta       = "✅ done" if done else _eta_str(remaining, copied, elapsed)

        status_icon = "✅" if done else "⚡"
        return (
            f"{status_icon} *{label}*\n"
            f"`[{bar}]` {pct}%\n"
            f"✅ `{copied:,}`  ⏭ `{skipped:,}`  ❌ `{failed:,}`\n"
            f"📦 Assigned: `{assigned:,}` msgs\n"
            f"🚀 Rate: `{rate}`  •  🏁 ETA: `{eta}`"
        )

    b1_block = _bot_block("Bot 1 (first half)",  b1)
    b2_block = _bot_block("Bot 2 (second half)", b2)

    # ── combined totals ───────────────────────────────────────────────────────
    total_copied  = shared["copied"]
    total_skipped = shared["skipped"]
    total_failed  = shared["failed"]
    processed_all = total_copied + total_skipped + total_failed
    remaining_all = max(0, total - processed_all)
    overall_pct   = min(100, int(processed_all / max(total, 1) * 100))
    overall_bar   = _mini_bar(processed_all, total, width=20)
    overall_rate  = _rate_str(total_copied, elapsed)
    overall_eta   = _eta_str(remaining_all, total_copied, elapsed)

    elapsed_int   = int(elapsed)
    e_m, e_s      = divmod(elapsed_int, 60)
    e_h, e_m      = divmod(e_m, 60)
    elapsed_str   = (f"{e_h}h {e_m}m {e_s}s" if e_h else
                     f"{e_m}m {e_s}s"         if e_m else
                     f"{e_s}s")

    text = (
        f"📊 *Dual-Copy Status*\n"
        f"📡 `{src}` → `{dst}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{b1_block}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{b2_block}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 *Combined*\n"
        f"`[{overall_bar}]` {overall_pct}%\n"
        f"✅ `{total_copied:,}`  ⏭ `{total_skipped:,}`  ❌ `{total_failed:,}`  /  `{total:,}` total\n"
        f"🚀 Rate: `{overall_rate}`  •  🏁 ETA: `{overall_eta}`\n"
        f"⏱ Elapsed: `{elapsed_str}`"
    )

    await update.message.reply_text(text, parse_mode="Markdown")
