"""
Dual-bot parallel copy engine.

Two Telethon clients (Bot 1 and Bot 2) copy files from the same source
channel to the same destination simultaneously:

  • The full message-ID range is collected once (oldest → newest).
  • The range is split in half: Bot 1 takes the first half, Bot 2 takes the second.
  • Both halves run concurrently as asyncio tasks.
  • A shared asyncio.Lock protects writes to the shared checkpoint so neither
    bot ever copies the same message twice.
  • A shared stats dict accumulates totals from both halves for progress reporting.

Checkpoint format is identical to the single-bot checkpoint so existing
checkpoints are forward-compatible (a /copy checkpoint can be resumed by
/dualcopy and vice-versa).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from telethon import TelegramClient

from . import checkpoint as ckpt
from .sender import send_album, _do_send
from .filter_utils import matches_filter

logger = logging.getLogger(__name__)

SAVE_EVERY  = 25
BATCH_SIZE  = 100   # how many IDs to fetch at once with get_messages()


# ─── helpers ──────────────────────────────────────────────────────────────────

def _entity_id(entity) -> int:
    return abs(getattr(entity, "id", 0))


async def _collect_message_ids(
    client: TelegramClient,
    source_entity,
    min_id: int = 0,
) -> list[int]:
    """
    Fetch all message IDs from the source channel in ascending order.
    Skips IDs at or below min_id (already copied in a previous run).
    """
    ids = []
    async for msg in client.iter_messages(source_entity, reverse=True, min_id=min_id):
        ids.append(msg.id)
    return ids   # ascending order (reverse=True, no offset, oldest first)


async def _flush_album(
    gid: int,
    album_buf: dict,
    album_order: list,
    client: TelegramClient,
    dest_entity,
    state: dict,
    lock: asyncio.Lock,
    shared_stats: dict,
    allowed_exts,
    caption_replacement: str,
    skip_text: bool,
    dry_run: bool,
):
    """
    Send a buffered album group.  All mutable state is passed explicitly so
    there are no closure-over-loop bugs.
    """
    msgs = album_buf.pop(gid, [])
    if gid in album_order:
        album_order.remove(gid)
    if not msgs:
        return

    async with lock:
        if all(m.id in state["copied_ids"] for m in msgs):
            shared_stats["skipped"] += len(msgs)
            return
        if not any(matches_filter(m, allowed_exts, skip_text=False) for m in msgs):
            shared_stats["skipped"] += len(msgs)
            return

    result = await send_album(
        client, dest_entity, msgs,
        dry_run=dry_run,
        caption_replacement=caption_replacement,
        on_flood_wait=None,
    )

    async with lock:
        if result == "ok":
            shared_stats["copied"] += len(msgs)
            state["last_msg_id"] = max(state["last_msg_id"], msgs[-1].id)
            for m in msgs:
                state["copied_ids"].add(m.id)
        elif result == "fail":
            shared_stats["failed"] += len(msgs)
        else:
            shared_stats["skipped"] += len(msgs)


# ─── per-bot copy worker ──────────────────────────────────────────────────────

async def _copy_range(
    label: str,
    client: TelegramClient,
    source_entity,
    dest_entity,
    message_ids: list[int],
    state: dict,
    lock: asyncio.Lock,
    shared_stats: dict,
    allowed_exts,
    caption_replacement: str,
    skip_text: bool,
    dry_run: bool,
    stop_event: asyncio.Event,
    source_id: int,
    dest_id: int,
):
    """
    Copy a slice of message IDs using one Telethon client.
    Messages are fetched in batches of BATCH_SIZE for efficiency.
    """
    processed = 0
    last_save = time.time()
    album_buf:   dict[int, list] = {}
    album_order: list[int]       = []

    flush_kwargs = dict(
        album_buf           = album_buf,
        album_order         = album_order,
        client              = client,
        dest_entity         = dest_entity,
        state               = state,
        lock                = lock,
        shared_stats        = shared_stats,
        allowed_exts        = allowed_exts,
        caption_replacement = caption_replacement,
        skip_text           = skip_text,
        dry_run             = dry_run,
    )

    async def _save():
        async with lock:
            state.update({
                "copied":      shared_stats["copied"],
                "skipped":     shared_stats["skipped"],
                "failed":      shared_stats["failed"],
                "flood_waits": shared_stats["flood_waits"],
            })
            ckpt.save(source_id, dest_id, state)

    try:
        for batch_start in range(0, len(message_ids), BATCH_SIZE):
            if stop_event.is_set():
                break

            batch_ids = message_ids[batch_start : batch_start + BATCH_SIZE]

            # Fetch the actual message objects for this batch
            try:
                batch_messages = await client.get_messages(source_entity, ids=batch_ids)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{label} get_messages batch failed: {e}")
                async with lock:
                    shared_stats["failed"] += len(batch_ids)
                continue

            # Iterate the batch — group albums, send singles
            for message in batch_messages:
                if stop_event.is_set():
                    break

                if message is None:
                    # Deleted or inaccessible message — count as skipped
                    async with lock:
                        shared_stats["skipped"] += 1
                    processed += 1
                    continue

                gid = message.grouped_id

                if gid:
                    if gid not in album_buf:
                        album_buf[gid] = []
                        album_order.append(gid)
                    album_buf[gid].append(message)

                    # Flush any other pending album (different grouped_id)
                    for old_gid in list(album_order):
                        if old_gid != gid:
                            await _flush_album(old_gid, **flush_kwargs)

                    processed += 1
                    await asyncio.sleep(0)
                    continue

                else:
                    # Flush any pending albums before this single message
                    for old_gid in list(album_order):
                        await _flush_album(old_gid, **flush_kwargs)

                    # Dedup check
                    async with lock:
                        already = message.id in state["copied_ids"]

                    if already:
                        async with lock:
                            shared_stats["skipped"] += 1
                        processed += 1
                        await asyncio.sleep(0)
                        continue

                    # Filter check
                    if not matches_filter(message, allowed_exts, skip_text=skip_text):
                        async with lock:
                            shared_stats["skipped"] += 1
                        processed += 1
                        await asyncio.sleep(0)
                        continue

                    # Send
                    result = await _do_send(
                        client, dest_entity, message,
                        dry_run=dry_run,
                        caption_replacement=caption_replacement,
                        on_flood_wait=None,
                    )

                    async with lock:
                        if result == "ok":
                            shared_stats["copied"] += 1
                            state["last_msg_id"] = max(state["last_msg_id"], message.id)
                            state["copied_ids"].add(message.id)
                        elif result == "skip":
                            shared_stats["skipped"] += 1
                        else:
                            shared_stats["failed"] += 1

                processed += 1

                now = time.time()
                if processed % SAVE_EVERY == 0 or (now - last_save) > 30:
                    await _save()
                    last_save = now

                await asyncio.sleep(0)

            # End of batch — flush any remaining albums and save
            for gid in list(album_order):
                await _flush_album(gid, **flush_kwargs)

            now = time.time()
            if now - last_save > 10:
                await _save()
                last_save = now

    except asyncio.CancelledError:
        logger.info(f"{label} worker cancelled — saving checkpoint")
        for gid in list(album_order):
            msgs = album_buf.get(gid, [])
            if msgs:
                async with lock:
                    state["last_msg_id"] = max(state["last_msg_id"], msgs[0].id)
        await _save()
        raise

    except Exception as e:
        logger.exception(f"{label} worker error: {e}")
        await _save()
        raise

    # Clean exit save
    await _save()
    logger.info(f"{label} worker done — checkpoint saved")


# ─── public API ───────────────────────────────────────────────────────────────

async def copy_channel_files_dual(
    client1: TelegramClient,
    client2: TelegramClient,
    source,
    dest,
    force_restart: bool = False,
    allowed_exts=None,
    caption_replacement: str = "",
    skip_text: bool = False,
    dry_run: bool = False,
    progress_cb: Callable[[str, bool], Awaitable[None]] | None = None,
):
    """
    Copy source → dest using two Telethon clients in parallel.

    progress_cb(text, force) — async callable called with a status string.
        force=True → always edit immediately (used for start/finish messages).
    """
    # ── resolve entities ─────────────────────────────────────────────────────
    source_entity1 = await client1.get_entity(source)
    dest_entity1   = await client1.get_entity(dest)
    source_entity2 = await client2.get_entity(source)
    dest_entity2   = await client2.get_entity(dest)

    source_id   = _entity_id(source_entity1)
    dest_id     = _entity_id(dest_entity1)
    source_name = getattr(source_entity1, "title", str(source))
    dest_name   = getattr(dest_entity1,   "title", str(dest))

    # ── checkpoint ───────────────────────────────────────────────────────────
    if force_restart:
        ckpt.delete(source_id, dest_id)
        logger.info("Dual-copy: force restart — checkpoint deleted")

    state       = ckpt.load(source_id, dest_id)
    resume_from = state["last_msg_id"]

    logger.info(
        f"Dual-copy start: {source_name} → {dest_name} | "
        f"resume_from={resume_from} | already_copied={state['copied']}"
    )

    if progress_cb:
        await progress_cb(
            f"🚀 *Dual-Bot Copy*\n\n"
            f"📡 Source: `{source_name}`\n"
            f"📥 Dest:   `{dest_name}`\n\n"
            "⏳ Collecting message list…",
            True,
        )

    # ── collect all remaining message IDs ────────────────────────────────────
    all_ids = await _collect_message_ids(client1, source_entity1, min_id=resume_from)
    total   = len(all_ids)

    if total == 0:
        if progress_cb:
            await progress_cb(
                f"✅ *Dual-copy complete — nothing to do.*\n\n"
                f"📡 `{source_name}` → `{dest_name}`\n"
                "All messages already copied.",
                True,
            )
        return

    # ── split IDs 50/50 ──────────────────────────────────────────────────────
    mid      = total // 2
    ids_bot1 = all_ids[:mid]
    ids_bot2 = all_ids[mid:]

    logger.info(
        f"Dual-copy split: {total} msgs | "
        f"Bot1={len(ids_bot1)} | Bot2={len(ids_bot2)}"
    )

    if progress_cb:
        await progress_cb(
            f"🚀 *Dual-Bot Copy Running*\n\n"
            f"📡 Source: `{source_name}`\n"
            f"📥 Dest:   `{dest_name}`\n\n"
            f"📨 Total messages: `{total:,}`\n"
            f"🤖 Bot 1: `{len(ids_bot1):,}` (first half)\n"
            f"🤖 Bot 2: `{len(ids_bot2):,}` (second half)\n\n"
            "⚡ Both bots running in parallel…",
            True,
        )

    # ── shared primitives ─────────────────────────────────────────────────────
    lock         = asyncio.Lock()
    stop_event   = asyncio.Event()
    shared_stats = {
        "copied":      state.get("copied",      0),
        "skipped":     state.get("skipped",     0),
        "failed":      state.get("failed",      0),
        "flood_waits": state.get("flood_waits", 0),
    }
    started_at = time.time()

    # ── live progress reporter ────────────────────────────────────────────────
    async def _report_loop():
        while not stop_event.is_set():
            await asyncio.sleep(15)
            if stop_event.is_set():
                break
            copied  = shared_stats["copied"]
            skipped = shared_stats["skipped"]
            failed  = shared_stats["failed"]
            elapsed = int(time.time() - started_at)
            mins, secs = divmod(elapsed, 60)

            rate = copied / max(elapsed, 1)
            remaining_msgs = max(0, total - copied - skipped)
            eta_secs = int(remaining_msgs / rate) if rate > 0 else 0
            eta_m, eta_s = divmod(eta_secs, 60)

            pct        = min(100, int((copied + skipped) / max(total, 1) * 100))
            bar        = "█" * (pct // 5) + "░" * (20 - pct // 5)

            if progress_cb:
                await progress_cb(
                    f"⚡ *Dual-Bot Copy In Progress*\n\n"
                    f"📡 `{source_name}` → `{dest_name}`\n\n"
                    f"`[{bar}]` {pct}%\n\n"
                    f"✅ Copied:  `{copied:,}`\n"
                    f"⏭ Skipped: `{skipped:,}`\n"
                    f"❌ Failed:  `{failed:,}`\n"
                    f"📨 Total:   `{total:,}`\n\n"
                    f"⏱ Elapsed: `{mins}m {secs}s`\n"
                    f"🏁 ETA:     `{eta_m}m {eta_s}s`\n\n"
                    "🤖 Both bots running in parallel…",
                    False,
                )

    # ── launch both worker tasks ──────────────────────────────────────────────
    common = dict(
        state               = state,
        lock                = lock,
        shared_stats        = shared_stats,
        allowed_exts        = allowed_exts,
        caption_replacement = caption_replacement,
        skip_text           = skip_text,
        dry_run             = dry_run,
        stop_event          = stop_event,
        source_id           = source_id,
        dest_id             = dest_id,
    )
    worker1 = asyncio.create_task(
        _copy_range(
            label         = "Bot1",
            client        = client1,
            source_entity = source_entity1,
            dest_entity   = dest_entity1,
            message_ids   = ids_bot1,
            **common,
        )
    )
    worker2 = asyncio.create_task(
        _copy_range(
            label         = "Bot2",
            client        = client2,
            source_entity = source_entity2,
            dest_entity   = dest_entity2,
            message_ids   = ids_bot2,
            **common,
        )
    )
    reporter = asyncio.create_task(_report_loop())

    # ── wait for completion ───────────────────────────────────────────────────
    try:
        await asyncio.gather(worker1, worker2)
    except asyncio.CancelledError:
        stop_event.set()
        worker1.cancel()
        worker2.cancel()
        reporter.cancel()
        await asyncio.gather(worker1, worker2, reporter, return_exceptions=True)
        raise
    except Exception:
        stop_event.set()
        worker1.cancel()
        worker2.cancel()
        reporter.cancel()
        await asyncio.gather(worker1, worker2, reporter, return_exceptions=True)
        raise
    finally:
        stop_event.set()
        reporter.cancel()
        try:
            await reporter
        except asyncio.CancelledError:
            pass

    # ── done — report summary ─────────────────────────────────────────────────
    copied  = shared_stats["copied"]
    skipped = shared_stats["skipped"]
    failed  = shared_stats["failed"]
    elapsed = int(time.time() - started_at)
    mins, secs = divmod(elapsed, 60)

    summary = (
        f"✅ *Dual-Bot Copy Complete!*\n\n"
        f"📡 `{source_name}` → `{dest_name}`\n\n"
        f"✅ Copied:  `{copied:,}`\n"
        f"⏭ Skipped: `{skipped:,}`\n"
        f"❌ Failed:  `{failed:,}`\n"
        f"📨 Total:   `{total:,}`\n\n"
        f"⏱ Time: `{mins}m {secs}s`\n"
        f"⚡ Used *2 bots in parallel* for ~2× speed"
    )

    if progress_cb:
        await progress_cb(summary, True)

    logger.info(
        f"Dual-copy done: {source_name} → {dest_name} | "
        f"copied={copied} skipped={skipped} failed={failed} "
        f"time={mins}m{secs}s"
    )
