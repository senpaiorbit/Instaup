import os
import sys
import gc
import threading
from pathlib import Path

from modules.utils import load_config, setup_logger


PROJECT_ROOT = Path(__file__).resolve().parent


def _get_media_id(media):
    if hasattr(media, "id") and media.id is not None:
        return str(media.id)
    if hasattr(media, "pk") and media.pk is not None:
        return str(media.pk)
    if isinstance(media, dict):
        return str(media.get("id") or media.get("pk") or "")
    return None


def _start_healthcheck():
    # skip if already running via app.py (avoids PORT conflict)
    if os.environ.get("FROM_APP"):
        return
    import http.server
    import socketserver

    port = int(os.environ.get("PORT", 8080))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    try:
        srv = socketserver.TCPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except OSError:
        # PORT already in use (app.py running) - ignore
        pass


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Instagram reels auto-post bot")
    parser.add_argument("--config", default="config.json", help="Path to config file")
    args = parser.parse_args()

    config = load_config(args.config)
    if not config:
        print("Config file not found or empty")
        return 1

    logger = setup_logger(config.get("log_file", "data/logs/bot.log"), config.get("log_level", "INFO"))

    if os.environ.get("RENDER"):
        _start_healthcheck()

    from modules.auth import get_client
    from modules.dedup import filter_new, mark_uploaded
    from modules.scraper import fetch_reels
    from modules.uploader import upload_reel

    dedup_path = config.get("dedup_file", "data/uploaded.json")
    dedup_enabled = config.get("enable_dedup", config.get("dedup_enabled", True))
    if isinstance(dedup_path, bool) and not dedup_path:
        dedup_enabled = False
    max_reels = config.get("max_reels_per_run", 1)

    client = get_client(config, logger)
    if not client:
        logger.error("Login failed. No reels uploaded this run.")
        return 1

    medias = fetch_reels(client, config, logger)
    if not medias:
        logger.info("No reels found in the source feed")
        return 0

    if dedup_enabled:
        new = filter_new(medias, str(dedup_path), logger)
        if not new:
            logger.info("All reels are duplicates; nothing to upload")
            return 0
    else:
        logger.info("Dedup disabled (enable_dedup=false) - skipping duplicate check")
        new = medias

    uploaded = 0
    failed = 0
    for i, media in enumerate(new[:max_reels]):
        media_id = _get_media_id(media) or "unknown"
        try:
            ok = upload_reel(client, media, config, logger)
        except Exception as e:
            logger.error(f"Unexpected error uploading {media_id}: {e}")
            ok = False
        if ok and dedup_enabled:
            mark_uploaded(media_id, dedup_path)
            uploaded += 1
        elif ok:
            uploaded += 1
        else:
            failed += 1

        if i < len(new[:max_reels]) - 1:
            from modules.utils import human_delay
            human_delay(config, logger)

    gc.collect()

    logger.info(
        f"Summary: found {len(medias)}, new {len(new)}, uploaded {uploaded}, "
        f"failed {failed}, skipped {len(medias) - len(new)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
