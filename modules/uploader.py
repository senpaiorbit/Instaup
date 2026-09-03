import os
from pathlib import Path

from modules.scraper import get_video_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RENDER_TMP = Path(os.environ.get("TMPDIR", "/tmp"))
TMP_DIR = RENDER_TMP if RENDER_TMP.is_dir() else PROJECT_ROOT / "data" / "tmp"

try:
    import imageio_ffmpeg
    os.environ.setdefault("IMAGEIO_FFMPEG_EXE", imageio_ffmpeg.get_ffmpeg_exe())
    os.environ.setdefault("FFMPEG_BINARY", imageio_ffmpeg.get_ffmpeg_exe())
except Exception:
    pass


def _download_video(url, dest_path, logger=None):
    import requests

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(
            url,
            stream=True,
            timeout=(10, 30),
            headers={"Accept-Encoding": "identity"},
        ) as resp:
            resp.raise_for_status()
            content_len = int(resp.headers.get("content-length", 0))
            max_mb = 30
            try:
                from modules.utils import load_config
                max_mb = load_config().get("max_video_mb", 30)
            except Exception:
                pass
            if content_len and content_len > max_mb * 1024 * 1024:
                if logger:
                    logger.warning(f"Video too large ({content_len / 1048576:.1f}MB > {max_mb}MB), skipping")
                return None
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=32768):
                    f.write(chunk)
        return str(dest_path)
    except Exception as e:
        msg = f"Failed to download: {e}"
        if logger:
            logger.error(msg)
        else:
            print(msg)
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        return None


def build_caption(media, config):
    caption_mode = config.get("caption_mode", "original")
    raw = None
    if isinstance(media, dict):
        cap = media.get("caption")
        if isinstance(cap, dict):
            raw = cap.get("text")
        else:
            raw = media.get("caption_text") or cap
    else:
        raw = getattr(media, "caption_text", None)
    original = raw or ""

    if caption_mode == "original":
        return original
    if caption_mode == "custom":
        return config.get("custom_caption", original)

    hashtags = config.get("custom_hashtags", [])
    if hashtags and original:
        return f"{original}\n\n" + " ".join(f"#{h.strip('#')}" for h in hashtags)
    elif hashtags:
        return " ".join(f"#{h.strip('#')}" for h in hashtags)
    return original


def upload_reel(client, media, config, logger=None):
    media_id = getattr(media, "id", None) if hasattr(media, "id") else (media.get("id") if isinstance(media, dict) else "unknown")
    if not media_id or media_id == "unknown":
        media_id = getattr(media, "pk", None) or (media.get("pk") if isinstance(media, dict) else None) or "unknown"

    video_url = get_video_url(media, logger)
    if not video_url:
        msg = f"No video_url for {media_id}"
        if logger:
            logger.error(msg)
        else:
            print(msg)
        return False

    tmp_path = TMP_DIR / f"{media_id}.mp4"
    if logger:
        logger.info(f"Downloading {media_id} ...")
    result = _download_video(video_url, tmp_path, logger)
    if not result:
        return False
    if logger and tmp_path.exists():
        size_mb = tmp_path.stat().st_size / 1048576
        logger.info(f"Downloaded {size_mb:.2f} MB")

    caption = build_caption(media, config)

    custom_cover = (
        config.get("cover_path")
        or config.get("reel_cover")
        or config.get("custom_cover")
        or config.get("thumbnail")
        or config.get("thumbnail_path")
        or config.get("cover")
    )
    thumb_path = tmp_path.with_suffix(".jpg")
    thumb_arg = None
    if custom_cover:
        p = Path(custom_cover)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if p.is_dir():
            # support /cover/{n}.jpg|jpeg|png|webp - pick random
            import random
            candidates = list(p.glob("*.jpg")) + list(p.glob("*.jpeg")) + list(p.glob("*.png")) + list(p.glob("*.webp")) + list(p.glob("*.JPG")) + list(p.glob("*.JPEG")) + list(p.glob("*.PNG"))
            # also handle nested like cover/1.jpg, cover/2.png etc
            candidates = [c for c in candidates if c.is_file()]
            if candidates:
                p = random.choice(candidates)
        # handle pattern with wildcard like cover/* or cover/{n}
        if p.is_file():
            thumb_arg = str(p)
            if logger:
                logger.info(f"Using custom cover {p.name} (random from {p.parent.name}/)")
    if not thumb_arg:
        if not thumb_path.exists():
            try:
                import subprocess
                ffmpeg = os.environ.get("IMAGEIO_FFMPEG_EXE") or "ffmpeg"
                subprocess.run(
                    [ffmpeg, "-y", "-i", str(tmp_path), "-ss", "0.5", "-frames:v", "1", "-q:v", "2", str(thumb_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except Exception:
                pass
        thumb_arg = str(thumb_path) if thumb_path.exists() else None
    if logger:
        logger.info(f"Uploading {media_id} ...")

    try:
        if thumb_arg:
            client.clip_upload(path=str(tmp_path), caption=caption, thumbnail=Path(thumb_arg))
        else:
            client.clip_upload(path=str(tmp_path), caption=caption)
        if logger:
            logger.info(f"Uploaded {media_id}")
        else:
            print(f"Uploaded {media_id}")
        return True
    except Exception as e:
        msg = f"Upload failed {media_id}: {e}"
        if logger:
            logger.error(msg)
        else:
            print(msg)
        return False
    finally:
        for p in [tmp_path, thumb_path if 'thumb_path' in locals() else None]:
            try:
                if p and Path(p).exists():
                    Path(p).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            if TMP_DIR.exists() and not any(TMP_DIR.iterdir()):
                TMP_DIR.rmdir()
        except Exception:
            pass
