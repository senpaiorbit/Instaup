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


def _fetch_feed_items(client, logger=None, max_pages=3):
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

def _load_accounts(path: str, logger=None) -> list[str]:
    from pathlib import Path
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    if not p.exists():
        if logger:
            logger.warning(f"accounts file not found: {p}")
        return []
    accounts = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # strip @ and whitespace, handle @{username} or @username
        line = line.lstrip("@").strip("{} ").strip()
        if line:
            accounts.append(line)
    if logger:
        logger.info(f"Loaded {len(accounts)} accounts from {p.name}")
    return accounts

def _fetch_from_accounts(client, config: dict, logger=None):
    import random
    # support query override: ?account=@a,@b or ?accounts=@a& ?src=accounts (via CONFIG_OVERRIDE)
    accounts = config.get("accounts")
    if isinstance(accounts, list) and accounts:
        # normalize @ prefix and strip
        normalized = []
        for a in accounts:
            a = str(a).strip().lstrip("@").strip("{} ").strip()
            if a:
                normalized.append(a)
        accounts = normalized
        if logger:
            logger.info(f"Using query accounts override: {accounts}")
    else:
        accounts_file = config.get("accounts_file", "accounts.txt")
        accounts = _load_accounts(accounts_file, logger)
    if not accounts:
        if logger:
            logger.error("No accounts in accounts.txt and no query accounts")
        return []
    # log active filters
    filt = {k: config.get(k) for k in ("min_likes","min_comments","min_shares","min_views","max_age_hours") if config.get(k)}
    if filt and logger:
        logger.info(f"Accounts filters active: {filt}")
    # shuffle and try each until we get videos
    random.shuffle(accounts)
    own_id = str(getattr(client, "user_id", "") or "")
    for username in accounts[:5]:  # try up to 5 random accounts per run
        if logger:
            logger.info(f"Trying account @{username}...")
        try:
            user_id = client.user_id_from_username(username)
        except Exception as e:
            if logger:
                logger.warning(f"@{username} lookup failed: {e}")
            continue
        if own_id and str(user_id) == own_id:
            if logger:
                logger.info(f"Skipping own account @{username}")
            continue
        # fetch clips - try user_clips first (most efficient), fallback to user_medias
        medias = []
        try:
            # try clips (reels) endpoint
            if hasattr(client, "user_clips"):
                medias = client.user_clips(user_id, amount=12)
            elif hasattr(client, "user_clips_v1"):
                medias = client.user_clips_v1(user_id, amount=12)
            else:
                medias = client.user_medias(user_id, amount=12)
        except Exception as e:
            if logger:
                logger.warning(f"user_clips failed for @{username}: {e}")
            try:
                medias = client.user_medias(user_id, amount=12)
            except Exception as e2:
                if logger:
                    logger.warning(f"user_medias fallback failed @{username}: {e2}")
                continue
        # filter videos only and skip own/ads + engagement filters
        candidates = []
        for m in medias:
            if isinstance(m, dict):
                if not _is_video(m):
                    continue
                if not _passes_filters(m, config, logger):
                    continue
                m = _try_normalize(m)
            if not _is_video(m):
                continue
            if not _passes_filters(m, config, logger):
                continue
            try:
                uid = str(getattr(getattr(m, "user", None), "pk", "") or "")
                if own_id and uid == own_id:
                    continue
            except Exception:
                pass
            candidates.append(m)
        if not candidates:
            if logger:
                logger.info(f"@{username} has no clips passing filters, trying next")
            continue
        random.shuffle(candidates)
        if logger:
            logger.info(f"Picked @{username} with {len(candidates)} clips (filtered) -> returning {min(len(candidates), 6)}")
        # return up to 6 shuffled so dedup can find new (was 1, caused All duplicates)
        return candidates[:6]
    return []

def _passes_filters(media, config, logger=None):
    # config filters: min_likes, min_comments, min_shares, min_views, max_age_hours
    # supports both dict and Media object
    def get_val(obj, keys, default=0):
        for k in keys:
            if isinstance(obj, dict):
                v = obj.get(k)
            else:
                v = getattr(obj, k, None)
            if v is not None:
                try:
                    return int(v)
                except Exception:
                    return v
        return default
    def get_taken_at(obj):
        if isinstance(obj, dict):
            return obj.get("taken_at") or obj.get("taken_at_utc") or obj.get("created_at")
        return getattr(obj, "taken_at", None) or getattr(obj, "taken_at_utc", None)

    min_likes = int(config.get("min_likes", 0) or 0)
    if min_likes:
        likes = get_val(media, ["like_count", "likes"], 0)
        if likes < min_likes:
            if logger:
                logger.debug(f"Filter skip likes {likes} < {min_likes}")
            return False
    min_comments = int(config.get("min_comments", 0) or 0)
    if min_comments:
        comments = get_val(media, ["comment_count", "comments", "commenting_count"], 0)
        # fallback: preview_comments length
        if not comments and isinstance(media, dict):
            comments = len(media.get("preview_comments") or [])
        if comments < min_comments:
            if logger:
                logger.debug(f"Filter skip comments {comments} < {min_comments}")
            return False
    min_shares = int(config.get("min_shares", config.get("min_reposts", 0)) or 0)
    if min_shares:
        shares = get_val(media, ["reshare_count", "share_count", "reposts", "shares"], 0)
        if shares < min_shares:
            if logger:
                logger.debug(f"Filter skip shares {shares} < {min_shares}")
            return False
    min_views = int(config.get("min_views", 0) or 0)
    if min_views:
        views = get_val(media, ["play_count", "view_count", "views", "video_view_count"], 0)
        if views < min_views:
            if logger:
                logger.debug(f"Filter skip views {views} < {min_views}")
            return False
    max_age = config.get("max_age_hours")
    if max_age:
        try:
            max_age = float(max_age)
            taken = get_taken_at(media)
            if taken:
                import time
                age_h = (time.time() - float(taken)) / 3600
                if age_h > max_age:
                    if logger:
                        logger.debug(f"Filter skip age {age_h:.1f}h > {max_age}h")
                    return False
        except Exception:
            pass
    return True

def _filter_item(item, own_id, skipped, config=None, logger=None):
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
    # engagement filters on raw dict before normalize (cheaper)
    if config and not _passes_filters(raw, config, logger):
        skipped["filtered"] = skipped.get("filtered", 0) + 1
        return None
    media = _try_normalize(raw)
    try:
        u = getattr(media, "user", None)
        uid = str(getattr(u, "pk", "") or "") if u else ""
        if own_id and uid and uid == own_id:
            skipped["own"] += 1
            return None
    except Exception:
        pass
    # also check normalized Media (has proper like_count etc)
    if config and not _passes_filters(media, config, logger):
        skipped["filtered"] = skipped.get("filtered", 0) + 1
        return None
    return media


def fetch_reels(client, config: dict, logger=None) -> list:
    source = config.get("source", "feed")
    max_reels = config.get("max_reels_per_run", 1)
    fetch_count = max(max_reels * 2, 6)
    # helper to try until found (for strict filters)
    has_filters = any(config.get(k) for k in ("min_likes","min_comments","min_shares","min_views","max_age_hours"))

    if source == "accounts":
        medias = _fetch_from_accounts(client, config, logger)
        if logger:
            logger.info(f"Accounts source gave {len(medias)} videos")
        if not medias:
            if logger:
                logger.info("Accounts gave 0, falling back to feed")
            config = {**config, "source": "feed"}
            return fetch_reels(client, config, logger)
        return medias  # return all, dedup in main will pick new

    if source == "feed":
        feed_iter = _fetch_feed_items(client, logger)
        videos = []
        skipped = {"ads": 0, "own": 0, "filtered": 0}
        for item in feed_iter:
            media = _filter_item(item, str(getattr(client, "user_id", "") or ""), skipped, config, logger)
            if media:
                videos.append(media)
                if len(videos) >= fetch_count:
                    break
        if logger:
            logger.info(f"Filtered {len(videos)} videos from '{source}' (skipped {skipped['ads']} ads, {skipped['own']} own, {skipped.get('filtered',0)} filtered)")
        if not videos:
            if logger:
                logger.info("Feed had 0 organic videos, trying reels tray fallback...")
            fb_items = _fetch_reels_tray(client, logger)
            for item in fb_items:
                media = _filter_item(item, str(getattr(client, "user_id", "") or ""), skipped, config, logger)
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
                    media = _filter_item(item, str(getattr(client, "user_id", "") or ""), skipped, config, logger)
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
        skipped = {"ads": 0, "own": 0, "filtered": 0}
        for item in items:
            media = _filter_item(item, own_id, skipped, config, logger)
            if media:
                videos.append(media)
                if len(videos) >= fetch_count:
                    break
        if logger:
            logger.info(f"Filtered {len(videos)} videos from '{source}' (skipped {skipped['ads']} ads, {skipped['own']} own, {skipped.get('filtered',0)} filtered)")
    elif source in ("reels", "reels_tray", "explore_reels"):
        if source in ("reels", "reels_tray"):
            items = _fetch_reels_tray(client, logger)
        else:
            items = _fetch_explore_reels(client, amount=6, logger=logger)
        own_id = str(getattr(client, "user_id", "") or "")
        videos = []
        skipped = {"ads": 0, "own": 0, "filtered": 0}
        for item in items:
            media = _filter_item(item, own_id, skipped, config, logger)
            if media:
                videos.append(media)
                if len(videos) >= fetch_count:
                    break
        if logger:
            logger.info(f"Filtered {len(videos)} videos from '{source}' (skipped {skipped['ads']} ads, {skipped['own']} own, {skipped.get('filtered',0)} filtered)")
    else:
        if logger:
            logger.warning(f"Unknown source '{source}', falling back to feed")
        return fetch_reels(client, {**config, "source": "feed"}, logger)

    # shuffle so feed's different reels get chance (not just first in order) - fixes same reel repeat
    # also ensure we have more candidates than max_reels so dedup can find new
    if videos:
        import random as _rnd
        _rnd.shuffle(videos)
        # keep all filtered for dedup to choose from, not just fetch_count
        # dedup in main will pick first new after shuffle
    # return all filtered (not just max_reels) so dedup can find new among them
    # fallback until found: if strict filters gave 0, keep searching with larger fetch
    if not videos and has_filters:
        if logger:
            logger.info(f"No reels passed filters { {k:config.get(k) for k in ('min_likes','min_comments','min_shares','min_views','max_age_hours') if config.get(k)} } -> retrying with larger scan...")
        # try more pages / more clips (up to 3 retries)
        for attempt in range(2):
            more = []
            if source == "feed":
                # try more feed pages (5) and more clips
                for item in _fetch_feed_items(client, logger, max_pages=5):
                    m = _filter_item(item, str(getattr(client, "user_id", "") or ""), {"ads":0,"own":0,"filtered":0}, config, logger)
                    if m:
                        more.append(m)
                        if len(more) >= max_reels:
                            break
                if not more:
                    # try bigger clips discover
                    ex = _fetch_clips_discover(client, amount=20, logger=logger)
                    for it in ex:
                        m = _filter_item(it, str(getattr(client, "user_id", "") or ""), {"ads":0,"own":0,"filtered":0}, config, logger)
                        if m:
                            more.append(m)
                            if len(more) >= max_reels:
                                break
            elif source == "accounts":
                # try next random accounts (already tries 5, try again)
                more = _fetch_from_accounts(client, config, logger)
            else:
                more = _fetch_clips_discover(client, amount=20, logger=logger)
                # filter them
                tmp = []
                for it in more:
                    m = _filter_item(it, str(getattr(client, "user_id", "") or ""), {"ads":0,"own":0,"filtered":0}, config, logger)
                    if m:
                        tmp.append(m)
                more = tmp
            if more:
                if logger:
                    logger.info(f"Retry {attempt+1} found {len(more)} filtered reels")
                return more
            if logger:
                logger.info(f"Retry {attempt+1} still 0, trying again...")
    return videos  # let main handle max_reels after dedup
