def _best_video_url_from_versions(vv: list) -> str | None:
    if not isinstance(vv, list) or not vv:
        return None
    def score(v):
        try:
            bw = int(v.get("bandwidth") or 0)
            w = int(v.get("width") or 0)
            h = int(v.get("height") or 0)
            return bw * 1000000 + w * h
        except Exception:
            return 0
    best = sorted(vv, key=score, reverse=True)[0]
    url = best.get("url")
    if url:
        return str(url)
    return None

def get_video_url(media, logger=None) -> str | None:
    if isinstance(media, dict):
        if media.get("video_url"):
            return str(media["video_url"])
        vv = media.get("video_versions") or []
        url = _best_video_url_from_versions(vv) if vv else None
        if url and logger and isinstance(vv, list) and vv:
            try:
                best = sorted(vv, key=lambda v: int(v.get("bandwidth") or 0), reverse=True)[0]
                logger.info(f"Selected {best.get('width')}x{best.get('height')} bw={best.get('bandwidth')} from {len(vv)} versions")
            except Exception:
                pass
        if url:
            return url
        return None

    if hasattr(media, "video_url") and media.video_url:
        if logger and hasattr(media, "video_versions") and getattr(media, "video_versions"):
            try:
                vv = getattr(media, "video_versions")
                best = max(vv, key=lambda v: getattr(v, "bandwidth", 0))
                logger.info(f"Selected {best.width}x{best.height} bw={best.bandwidth} via Media object")
            except Exception:
                pass
        return str(media.video_url)

    if hasattr(media, "resources") and media.resources:
        for res in media.resources:
            if hasattr(res, "video_url") and res.video_url:
                return str(res.video_url)

    return None


def _is_video(media) -> bool:
    if isinstance(media, dict):
        if media.get("injected"):
            return False
        if media.get("product_type") == "ad":
            return False
        if media.get("ad_id") or media.get("label") == "Ad":
            return False
        if media.get("media_type") == 2:
            if media.get("product_type") not in (None, "clips", "clips_video", "feed", "video"):
                return False
            return True
        if media.get("product_type") in ("clips", "clips_video"):
            return True
        if isinstance(media.get("video_versions"), list) and media.get("video_versions"):
            return True
        return False

    if getattr(media, "product_type", None) == "ad":
        return False
    try:
        extra = getattr(media, "model_extra", None) or {}
        if extra.get("injected"):
            return False
    except Exception:
        pass
    if hasattr(media, "media_type") and media.media_type == 2:
        return True
    if hasattr(media, "product_type") and media.product_type in ("clips", "clips_video"):
        return True
    return False


def _try_normalize(media):
    if not isinstance(media, dict):
        return media
    try:
        from instagrapi.extractors import extract_media_v1
        return extract_media_v1(media)
    except Exception:
        return media

def _extract_media(item):
    if isinstance(item, dict):
        return item.get("media_or_ad") or item.get("media")
    if hasattr(item, "media_or_ad"):
        return item.media_or_ad
    if hasattr(item, "media"):
        return item.media
    return None


def _fetch_feed_items(client, logger=None, max_pages=2):
    next_max_id = None
    seen_posts = None
    for page in range(max_pages):
        try:
            resp = client.get_timeline_feed(max_id=next_max_id, seen_posts=seen_posts) if next_max_id else client.get_timeline_feed()
        except Exception as e:
            if logger:
                logger.warning(f"get_timeline_feed failed: {e}")
            try:
                resp = client.timeline_feed()
            except Exception as e2:
                if logger:
                    logger.error(f"timeline_feed fallback also failed: {e2}")
                break
        items = []
        if isinstance(resp, dict):
            items = resp.get("feed_items", [])
            next_max_id = resp.get("next_max_id")
            try:
                seen = []
                for it in items:
                    m = it.get("media_or_ad") or it.get("media")
                    if m and m.get("pk"):
                        seen.append(str(m.get("pk")))
                seen_posts = ",".join(seen) if seen else seen_posts
            except Exception:
                pass
        elif hasattr(resp, "feed_items"):
            items = resp.feed_items
            next_max_id = getattr(resp, "next_max_id", None)
        if not items:
            break
        for it in items:
            yield it
        if logger and page > 0:
            logger.info(f"Fetched page {page+1}: {len(items)} items")
        if not next_max_id:
            break


def _fetch_reels_tray(client, logger=None):
    try:
        tray = client.get_reels_tray_feed()
        items = []
        if isinstance(tray, dict):
            if "tray" in tray and isinstance(tray["tray"], list):
                for section in tray["tray"]:
                    if isinstance(section, dict):
                        if "media" in section:
                            items.append({"media_or_ad": section["media"]})
                        elif "items" in section:
                            for m in section["items"]:
                                items.append({"media_or_ad": m})
            elif "items" in tray:
                for m in tray["items"]:
                    items.append({"media_or_ad": m})
        if logger:
            logger.info(f"Reels tray fallback fetched {len(items)} items")
        return items
    except Exception as e:
        if logger:
            logger.warning(f"get_reels_tray_feed failed: {e}")
        return []

def _fetch_clips_discover(client, amount=6, logger=None):
    try:
        data = client.private_request("clips/discover/", {"num_result": amount})
        items = []
        raw_items = data.get("items_with_ads") or data.get("items") or []
        for it in raw_items:
            media = it.get("media") if isinstance(it, dict) else None
            if media:
                items.append({"media_or_ad": media})
            elif isinstance(it, dict) and it.get("pk"):
                items.append({"media_or_ad": it})
        if logger:
            logger.info(f"Clips discover fetched {len(items)} items")
        return items
    except Exception as e:
        if logger:
            logger.warning(f"clips/discover failed: {e}")
        return []

def _fetch_explore_reels(client, amount=6, logger=None):
    return _fetch_clips_discover(client, amount=amount, logger=logger)


def _filter_item(item, own_id, skipped):
    raw = _extract_media(item)
    if not raw:
        return None
    if not _is_video(raw):
        if isinstance(raw, dict) and (raw.get("injected") or raw.get("product_type") == "ad"):
            skipped["ads"] += 1
        return None
    if own_id:
        try:
            user_pk = str(raw.get("user", {}).get("pk") or raw.get("user", {}).get("id") or "")
            if user_pk and user_pk == own_id:
                skipped["own"] += 1
                return None
        except Exception:
            pass
    media = _try_normalize(raw)
    try:
        u = getattr(media, "user", None)
        uid = str(getattr(u, "pk", "") or "") if u else ""
        if own_id and uid and uid == own_id:
            skipped["own"] += 1
            return None
    except Exception:
        pass
    return media


def fetch_reels(client, config: dict, logger=None) -> list:
    source = config.get("source", "feed")
    max_reels = config.get("max_reels_per_run", 1)
    fetch_count = max(max_reels * 2, 6)

    if source == "feed":
        feed_iter = _fetch_feed_items(client, logger)
        videos = []
        skipped = {"ads": 0, "own": 0}
        for item in feed_iter:
            media = _filter_item(item, str(getattr(client, "user_id", "") or ""), skipped)
            if media:
                videos.append(media)
                if len(videos) >= fetch_count:
                    break
        if logger:
            logger.info(f"Filtered {len(videos)} videos from '{source}' (skipped {skipped['ads']} ads, {skipped['own']} own)")
        if not videos:
            if logger:
                logger.info("Feed had 0 organic videos, trying reels tray fallback...")
            fb_items = _fetch_reels_tray(client, logger)
            for item in fb_items:
                media = _filter_item(item, str(getattr(client, "user_id", "") or ""), skipped)
                if media:
                    videos.append(media)
                    if len(videos) >= fetch_count:
                        break
            if logger and videos:
                logger.info(f"Fallback reels tray gave {len(videos)} videos")
            if not videos:
                if logger:
                    logger.info("Trying explore_reels fallback...")
                ex_items = _fetch_explore_reels(client, amount=6, logger=logger)
                for item in ex_items:
                    media = _filter_item(item, str(getattr(client, "user_id", "") or ""), skipped)
                    if media:
                        videos.append(media)
                        if len(videos) >= max_reels:
                            break
    elif source == "explore":
        try:
            items = client.explore()
        except Exception as e:
            if logger:
                logger.error(f"explore() failed: {e}")
            return []
        own_id = str(getattr(client, "user_id", "") or "")
        videos = []
        skipped = {"ads": 0, "own": 0}
        for item in items:
            media = _filter_item(item, own_id, skipped)
            if media:
                videos.append(media)
                if len(videos) >= fetch_count:
                    break
        if logger:
            logger.info(f"Filtered {len(videos)} videos from '{source}' (skipped {skipped['ads']} ads, {skipped['own']} own)")
    elif source in ("reels", "reels_tray", "explore_reels"):
        if source in ("reels", "reels_tray"):
            items = _fetch_reels_tray(client, logger)
        else:
            items = _fetch_explore_reels(client, amount=6, logger=logger)
        own_id = str(getattr(client, "user_id", "") or "")
        videos = []
        skipped = {"ads": 0, "own": 0}
        for item in items:
            media = _filter_item(item, own_id, skipped)
            if media:
                videos.append(media)
                if len(videos) >= fetch_count:
                    break
        if logger:
            logger.info(f"Filtered {len(videos)} videos from '{source}' (skipped {skipped['ads']} ads, {skipped['own']} own)")
    else:
        if logger:
            logger.warning(f"Unknown source '{source}', falling back to feed")
        return fetch_reels(client, {**config, "source": "feed"}, logger)

    return videos[:max_reels]
