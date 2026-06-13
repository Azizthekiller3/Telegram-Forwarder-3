"""
/dualcopy  — Dual-bot parallel copy wizard (interactive, like /copy).
/stopdual  — Cancel the running dual-copy job.
/status2   — Per-bot live progress breakdown (Bot 1 vs Bot 2 side-by-side).

Wizard flow:
  1. /dualcopy          → check bots ready → ask for source
  2. Source typed       → ask for dest
  3. Dest typed         → show options keyboard
  4. Keyboard buttons:  skip-text / file-filter / replace / force-restart / start / cancel
  5. Replace typed      → back to options keyboard
  6. Start pressed      → launch background dual-copy task → END

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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import userbot_bridge as bridge
from userbot.dual_forwarder import copy_channel_files_dual, DUAL_STATUS_KEY
from userbot.filter_utils import parse_ext_filter
from states import (
    DUAL_AWAIT_SRC,
    DUAL_AWAIT_DST,
    DUAL_OPTIONS,
    DUAL_AWAIT_REPLACE,
)

logger = logging.getLogger(__name__)

_DUAL_TASK_KEY  = "active_dual_copy_task"

_FILTER_CYCLE = ["ALL", "mkv", "mp4", "mkv,mp4", "mkv,mp4,avi"]


# ─── wizard option helpers ─────────────────────────────────────────────────────

def _default_dual_opts() -> dict:
    return {
        "skip_text":           False,
        "filter_idx":          0,
        "filter_label":        "ALL",
        "allowed_exts":        set(),
        "caption_replacement": "",
        "force_restart":       False,
    }


def _dual_opts_keyboard(opts: dict) -> InlineKeyboardMarkup:
    skip_lbl = (
        f"{'✅' if opts['skip_text'] else '📝'} "
        f"Text posts: {'SKIP' if opts['skip_text'] else 'INCLUDE'}"
    )
    filter_lbl = f"📁 Filter: {opts['filter_label']}"
    repl_cur   = opts["caption_replacement"]
    repl_lbl   = (f"✏️ @username → {repl_cur}"
                  if repl_cur else "✏️ @username → keep original")
    restart_icon  = "🔄" if opts["force_restart"] else "▶"
    restart_label = "Force restart (ignore checkpoint)" if opts["force_restart"] else "Resume from checkpoint"
    restart_lbl   = f"{restart_icon} {restart_label}"

    rows = [
        [InlineKeyboardButton(skip_lbl,    callback_data="dopt_skip")],
        [InlineKeyboardButton(filter_lbl,  callback_data="dopt_filter")],
        [InlineKeyboardButton(repl_lbl,    callback_data="dopt_replace")],
        [InlineKeyboardButton(restart_lbl, callback_data="dopt_restart")],
        [
            InlineKeyboardButton("⚡ Start Dual Copy", callback_data="dopt_start"),
            InlineKeyboardButton("❌ Cancel",           callback_data="dopt_cancel"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _dual_opts_text(src_raw: str, dst_raw: str, opts: dict) -> str:
    return (
        f"⚙️ *Dual-Bot Copy Settings*\n\n"
        f"📡 Source: `{src_raw}`\n"
        f"📥 Dest:   `{dst_raw}`\n"
        f"🤖 Uses *2 accounts simultaneously* (~2× speed)\n\n"
        "Tap to change options, then tap *Start*:"
    )


# ─── wizard: entry point (/dualcopy) ──────────────────────────────────────────

async def dualcopy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /dualcopy — start the dual-bot copy wizard.

    Checks both userbots are ready, then walks the user through:
      source channel → dest channel → options → launch
    """
    bot_data = context.bot_data

    if not bridge.is_ready(bot_data):
        await update.message.reply_text(
            "❌ *Userbot 1 is not connected.*\n\n"
            "Use /login to connect your first account before using /dualcopy.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if not bridge.is_ready2(bot_data):
        await update.message.reply_text(
            "❌ *Userbot 2 is not connected.*\n\n"
            "Use /login2 to connect your second account before using /dualcopy.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    existing = bot_data.get(_DUAL_TASK_KEY)
    if existing and not existing.done():
        await update.message.reply_text(
            "⚠️ A dual-copy job is already running.\n"
            "Use /stopdual to cancel it first, or /status2 to see progress.",
        )
        return ConversationHandler.END

    existing_single = bot_data.get("active_copy_task")
    if existing_single and not existing_single.done():
        await update.message.reply_text(
            "⚠️ A single-bot copy job is already running.\n"
            "Use /stopjob to cancel it first.",
        )
        return ConversationHandler.END

    context.user_data["dual_opts"] = _default_dual_opts()

    await update.message.reply_text(
        "🤖 *Dual-Bot Copy Wizard*\n\n"
        "Both your Telegram accounts will upload simultaneously (~2× speed).\n\n"
        "Enter the *source* channel ID or @username:\n"
        "_(e.g. `-1001811670072` or `@mychannel`)_",
        parse_mode="Markdown",
    )
    return DUAL_AWAIT_SRC


# ─── wizard: step 1 — got source ──────────────────────────────────────────────

async def got_dual_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        context.user_data["dual_src"] = int(text)
    except ValueError:
        context.user_data["dual_src"] = text
    context.user_data["dual_src_raw"] = text

    await update.message.reply_text(
        "📥 Enter the *destination* channel ID or @username:\n"
        "_(e.g. `-1003563437550` or `@mybackup`)_",
        parse_mode="Markdown",
    )
    return DUAL_AWAIT_DST


# ─── wizard: step 2 — got dest → show options ─────────────────────────────────

async def got_dual_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        context.user_data["dual_dst"] = int(text)
    except ValueError:
        context.user_data["dual_dst"] = text
    context.user_data["dual_dst_raw"] = text

    opts    = context.user_data["dual_opts"]
    src_raw = context.user_data["dual_src_raw"]
    dst_raw = context.user_data["dual_dst_raw"]

    msg = await update.message.reply_text(
        _dual_opts_text(src_raw, dst_raw, opts),
        parse_mode="Markdown",
        reply_markup=_dual_opts_keyboard(opts),
    )
    context.user_data["dual_opts_msg_id"] = msg.message_id
    return DUAL_OPTIONS


# ─── wizard: step 3 — options keyboard ────────────────────────────────────────

async def dual_options_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    opts = context.user_data.get("dual_opts")
    if opts is None:
        await query.edit_message_text(
            "⚠️ Session expired. Please use /dualcopy to start again."
        )
        return ConversationHandler.END

    src_raw = context.user_data.get("dual_src_raw", "?")
    dst_raw = context.user_data.get("dual_dst_raw", "?")

    if data == "dopt_cancel":
        context.user_data.pop("dual_opts",    None)
        context.user_data.pop("dual_src",     None)
        context.user_data.pop("dual_src_raw", None)
        context.user_data.pop("dual_dst",     None)
        context.user_data.pop("dual_dst_raw", None)
        await query.edit_message_text("❌ Dual-copy cancelled.")
        return ConversationHandler.END

    elif data == "dopt_skip":
        opts["skip_text"] = not opts["skip_text"]

    elif data == "dopt_filter":
        opts["filter_idx"]   = (opts["filter_idx"] + 1) % len(_FILTER_CYCLE)
        opts["filter_label"] = _FILTER_CYCLE[opts["filter_idx"]]
        raw = opts["filter_label"]
        opts["allowed_exts"] = parse_ext_filter(raw) if raw != "ALL" else set()

    elif data == "dopt_replace":
        cur     = opts.get("caption_replacement", "")
        cur_disp = cur if cur else "off"
        await query.edit_message_text(
            f"✏️ *Caption Username Replacement*\n\n"
            f"Currently: `{cur_disp}`\n\n"
            "Every `@username` and `t.me/` link in captions will be replaced with "
            "whatever you enter here.\n\n"
            "Send the replacement (e.g. `@mychannel`), or send `off` to disable.",
            parse_mode="Markdown",
        )
        return DUAL_AWAIT_REPLACE

    elif data == "dopt_restart":
        opts["force_restart"] = not opts["force_restart"]

    elif data == "dopt_start":
        await _launch_dual_job(query, context, opts, src_raw, dst_raw)
        return ConversationHandler.END

    await query.edit_message_text(
        _dual_opts_text(src_raw, dst_raw, opts),
        parse_mode="Markdown",
        reply_markup=_dual_opts_keyboard(opts),
    )
    return DUAL_OPTIONS


# ─── wizard: step 4 — got replacement username ────────────────────────────────

async def got_dual_replace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    opts = context.user_data.get("dual_opts", {})

    if text.lower() in ("off", "none", "-", ""):
        opts["caption_replacement"] = ""
        confirm = "✅ Username replacement *disabled* — original links will be kept."
    else:
        username = text if text.startswith("@") else f"@{text}"
        opts["caption_replacement"] = username
        confirm = f"✅ All `@username` mentions and `t.me/` links will be replaced with `{username}`."

    src_raw = context.user_data.get("dual_src_raw", "?")
    dst_raw = context.user_data.get("dual_dst_raw", "?")

    await update.message.reply_text(
        confirm + "\n\n" + _dual_opts_text(src_raw, dst_raw, opts),
        parse_mode="Markdown",
        reply_markup=_dual_opts_keyboard(opts),
    )
    return DUAL_OPTIONS


# ─── wizard: cancel via /cancel command ───────────────────────────────────────

async def cancel_dual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("dual_opts", "dual_src", "dual_src_raw", "dual_dst", "dual_dst_raw"):
        context.user_data.pop(key, None)
    await update.message.reply_text(
        "❌ Dual-copy wizard cancelled. Use /dualcopy to start again."
    )
    return ConversationHandler.END


# ─── wizard: job launcher ─────────────────────────────────────────────────────

async def _launch_dual_job(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    opts: dict,
    src_raw: str,
    dst_raw: str,
):
    """Called when the user taps ⚡ Start Dual Copy in the options keyboard."""
    bot_data = context.bot_data
    bot      = context.application.bot
    chat_id  = query.message.chat_id
    client1  = bridge.get_client(bot_data)
    client2  = bridge.get_client2(bot_data)
    src      = context.user_data["dual_src"]
    dst      = context.user_data["dual_dst"]

    ext_label = (
        ", ".join(sorted(opts["allowed_exts"])).upper()
        if opts["allowed_exts"] else "ALL"
    )
    restart_note = "\n🔄 Force restart: yes" if opts["force_restart"] else ""

    await query.edit_message_text(
        f"⚡ *Dual-Bot Copy Launched!*\n\n"
        f"📡 Source: `{src_raw}`\n"
        f"📥 Dest:   `{dst_raw}`\n"
        f"📁 Filter: `{ext_label}`{restart_note}\n\n"
        "_Counting messages and splitting workload…_",
        parse_mode="Markdown",
    )

    status_msg = await bot.send_message(
        chat_id,
        "⏳ *Dual-Bot Copy Starting…*\n\nBoth bots are connecting to the source…",
        parse_mode="Markdown",
    )

    task = asyncio.create_task(
        _run_dual_copy(
            bot          = bot,
            chat_id      = chat_id,
            msg_id       = status_msg.message_id,
            bot_data     = bot_data,
            client1      = client1,
            client2      = client2,
            source_id    = src,
            dest_id      = dst,
            src_name     = src_raw,
            dst_name     = dst_raw,
            force_restart       = opts["force_restart"],
            allowed_exts        = opts["allowed_exts"] or None,
            caption_replacement = opts["caption_replacement"],
            skip_text           = opts["skip_text"],
        )
    )
    bot_data[_DUAL_TASK_KEY] = task
    logger.info(f"Dual-copy task started: {src} → {dst}")


# ─── background runner ────────────────────────────────────────────────────────

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


def _eta_str(remaining: int, processed: int, elapsed: float) -> str:
    """ETA using processing rate (all outcomes, not just copies)."""
    if processed == 0 or elapsed < 1:
        return "?"
    rate = processed / elapsed
    secs = int(remaining / rate)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _mini_bar(done: int, total: int, width: int = 10) -> str:
    pct    = min(100, int(done / max(total, 1) * 100))
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

    def _bot_block(label: str, s: dict) -> str:
        assigned  = s["total_assigned"]
        copied    = s["copied"]
        skipped   = s["skipped"]
        failed    = s["failed"]
        done_flag = s.get("done", False)
        processed = copied + skipped + failed
        remaining = max(0, assigned - processed)

        bar      = _mini_bar(processed, assigned)
        pct      = min(100, int(processed / max(assigned, 1) * 100))
        rate     = _rate_str(copied, elapsed)
        eta      = "✅ done" if done_flag else _eta_str(remaining, processed, elapsed)
        icon     = "✅" if done_flag else "⚡"

        return (
            f"{icon} *{label}*\n"
            f"`[{bar}]` {pct}%\n"
            f"✅ `{copied:,}`  ⏭ `{skipped:,}`  ❌ `{failed:,}`\n"
            f"📦 Assigned: `{assigned:,}` msgs\n"
            f"🚀 Rate: `{rate}`  •  🏁 ETA: `{eta}`"
        )

    b1_block = _bot_block("Bot 1 (first half)",  b1)
    b2_block = _bot_block("Bot 2 (second half)", b2)

    total_copied  = shared["copied"]
    total_skipped = shared["skipped"]
    total_failed  = shared["failed"]
    processed_all = total_copied + total_skipped + total_failed
    remaining_all = max(0, total - processed_all)
    overall_pct   = min(100, int(processed_all / max(total, 1) * 100))
    overall_bar   = _mini_bar(processed_all, total, width=20)
    overall_rate  = _rate_str(total_copied, elapsed)
    overall_eta   = _eta_str(remaining_all, processed_all, elapsed)

    elapsed_int = int(elapsed)
    e_m, e_s    = divmod(elapsed_int, 60)
    e_h, e_m    = divmod(e_m, 60)
    elapsed_str = (f"{e_h}h {e_m}m {e_s}s" if e_h else
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
        f"✅ `{total_copied:,}`  ⏭ `{total_skipped:,}`  ❌ `{total_failed:,}`"
        f"  /  `{total:,}` total\n"
        f"🚀 Rate: `{overall_rate}`  •  🏁 ETA: `{overall_eta}`\n"
        f"⏱ Elapsed: `{elapsed_str}`"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


# ─── ConversationHandler builder ──────────────────────────────────────────────

def build_dualcopy_conv() -> ConversationHandler:
    """
    Build and return the /dualcopy wizard ConversationHandler.
    Must be registered BEFORE the main conv so /dualcopy always intercepts.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("dualcopy", dualcopy_cmd)],
        allow_reentry=True,
        states={
            DUAL_AWAIT_SRC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_dual_source),
            ],
            DUAL_AWAIT_DST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_dual_dest),
            ],
            DUAL_OPTIONS: [
                CallbackQueryHandler(
                    dual_options_callback,
                    pattern=r"^dopt_(skip|filter|replace|restart|start|cancel)$",
                ),
            ],
            DUAL_AWAIT_REPLACE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_dual_replace),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_dual),
            CommandHandler("dualcopy", dualcopy_cmd),
        ],
        per_chat=False,
        per_user=True,
        per_message=False,
    )
