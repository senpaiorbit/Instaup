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

    # apply query overrides from /upload?src=...&account=... without recommit (via CONFIG_OVERRIDE env)
    override_raw = os.environ.get("CONFIG_OVERRIDE")
    if override_raw:
        try:
            import json as _json
            over = _json.loads(override_raw)
            if isinstance(over, dict):
                # normalize aliases: src -> source, account -> accounts
                if "src" in over and "source" not in over:
                    over["source"] = over.pop("src")
                if "account" in over and "accounts" not in over:
                    # account may be string or list
                    acc = over.pop("account")
                    over["accounts"] = acc if isinstance(acc, list) else [acc]
                if "accounts" in over and isinstance(over["accounts"], str):
                    over["accounts"] = [over["accounts"]]
                # cover alias
                if "cover" in over and "cover_path" not in over:
                    over["cover_path"] = over.pop("cover")
                # type coercion for query strings (all come as strings from URL) - strip quotes
                for k in list(over.keys()):
                    v = over[k]
                    if isinstance(v, str):
                        v = v.strip().strip('"').strip("'").strip()
                        over[k] = v
                        low = v.lower()
                        if low in ("true", "false"):
                            over[k] = low == "true"
                        elif k in ("max_reels_per_run", "max_reels", "min_likes", "min_comments", "min_shares", "min_reposts", "min_views"):
                            try:
                                # strip quotes again and handle like '0"' 
                                clean = v.strip().strip('"').strip("'").strip()
                                over[k] = int(clean) if clean else 0
                            except Exception:
                                over[k] = 0
                                pass
                        elif k in ("delay_min", "delay_max", "max_age_hours"):
                            try:
                                clean = v.strip().strip('"').strip("'").strip()
                                over[k] = float(clean) if clean else 0
                            except Exception:
                                over[k] = 0
                                pass
                        elif k == "custom_hashtags":
                            over[k] = [x.strip().lstrip("#") for x in v.split(",") if x.strip()]
                        elif k == "accounts" and "," in v:
                            over[k] = [x.strip() for x in v.split(",") if x.strip()]
                config.update(over)
                # if accounts override given, force source to accounts
                if "accounts" in over:
                    config["source"] = "accounts"
        except Exception as e:
            print(f"Invalid CONFIG_OVERRIDE: {e}")
        finally:
            os.environ.pop("CONFIG_OVERRIDE", None)

    logger = setup_logger(config.get("log_file", "data/logs/bot.log"), config.get("log_level", "INFO"))
    # log effective config for live view
    try:
        filt = {k: config.get(k) for k in ("min_likes","min_comments","min_shares","min_views","max_age_hours") if config.get(k)}
        logger.info(f"Effective config: source={config.get('source')} accounts={config.get('accounts') or config.get('accounts_file')} cover={config.get('cover_path')} filters={filt if filt else 'none'}")
    except Exception:
        pass

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
            logger.info("All reels are duplicates; nothing to upload - trying fallback for new reel (instagram is infinite)...")
            # try up to 3 more fetches (random account / next page) until we find new
            for attempt in range(3):
                more = fetch_reels(client, config, logger)
                if not more:
                    continue
                new = filter_new(more, str(dedup_path), logger)
                if new:
                    medias = more
                    logger.info(f"Fallback attempt {attempt+1} found {len(new)} new")
                    break
                logger.info(f"Fallback {attempt+1} still all duplicates, retrying...")
            if not new:
                logger.info("All reels are duplicates after retries; nothing to upload")
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
