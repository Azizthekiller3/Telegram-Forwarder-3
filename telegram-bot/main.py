import logging
import os
import threading
import warnings

warnings.filterwarnings("ignore", message="If 'per_message=False'", category=UserWarning)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class _HealthHandler:
    """Minimal HTTP/1.1 health-check handler — avoids BaseHTTPRequestHandler
    overhead and keeps the response deterministic."""

    def __init__(self, conn, addr):
        try:
            conn.recv(4096)          # drain the request
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n"
                b"\r\nOK"
            )
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _start_health_server() -> bool:
    """
    Bind the health-check TCP socket and start a background thread.
    Returns True on success, False if the port is already in use.

    IMPORTANT: the socket is bound here (before thread.start) so that
    Railway's probe gets a valid response the instant the process starts —
    not after a thread-scheduling delay.
    """
    import socket

    port = int(os.environ.get("PORT", 8080))
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(16)
        logger.info("Health server bound on 0.0.0.0:%d", port)
    except OSError as exc:
        logger.error("Health server could not bind to port %d: %s", port, exc)
        return False

    def _serve():
        while True:
            try:
                conn, addr = srv.accept()
                _HealthHandler(conn, addr)
            except Exception:
                pass

    t = threading.Thread(target=_serve, daemon=False, name="health-server")
    t.start()
    return True


def main():
    # ── 1. Bind health server FIRST ──────────────────────────────────────────
    # Do this before ANY other import so Railway's probe never gets
    # "connection refused", even if the bot fails to start.
    if not _start_health_server():
        logger.critical("Cannot start without a health server — exiting")
        return

    # ── 2. NOW import bot code (safe: health server already responding) ──────
    # Moving imports here means a missing dependency or bad env var never
    # prevents the health server from binding.
    try:
        from bot import build_app
        from telegram import Update
    except Exception as exc:
        logger.critical("Import error — bot will NOT start: %s", exc, exc_info=True)
        logger.info("Health server is still running; process will stay alive.")
        threading.Event().wait()
        return

    # ── 3. Check token ────────────────────────────────────────────────────────
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set — bot will not start. "
            "Health server is still alive."
        )
        threading.Event().wait()
        return

    # ── 4. Build and run the bot ──────────────────────────────────────────────
    try:
        app = build_app(token)
        logger.info("Starting Telegram Forwarder Bot…")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    except Exception as exc:
        logger.critical(
            "Bot crashed — health server is still alive for inspection: %s",
            exc, exc_info=True,
        )
        threading.Event().wait()


if __name__ == "__main__":
    main()
