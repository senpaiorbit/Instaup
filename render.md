# InstaUp — Render Deploy Guide

Free plan (512 MB, Oregon) — optimized for low memory/bandwidth.

## 1. Push to GitHub
```bash
git add .
git commit -m "deploy"
git push origin main
```

## 2. Render Dashboard
- **New + → Web Service** → Connect `Instaup` repo
  - **Runtime:** `Python 3`
  - **Build Command:** `bash build.sh`  *(uses --prefer-binary + --no-deps to keep Pillow 12.2 with moviepy)*
  - **Start Command:** `python app.py`
  - **Plan:** `Free`
  - **Region:** `Oregon`
  - **Env Vars:**
    - `PYTHON_VERSION=3.11.11`
    - `RENDER=true`
    - `PYTHONUNBUFFERED=1`
    - `PIP_PREFER_BINARY=1`
  - **Health Check Path:** `/health`

- **New + → Cron Job** (optional auto every 6h)
  - **Build Command:** `bash build.sh`
  - **Start Command:** `python main.py`
  - **Schedule:** `0 */6 * * *` (every 6 hours)
  - Same env vars as above

## 3. Required Files
- `session.json` — instagrapi session (Pixel 8 Pro). Keep private, add to `.gitignore` if needed and upload via Render **Secret File** at `session.json`
- `config.json` — see below
- `accounts.txt` — when `source: accounts`
- `cover/1.jpg` — custom reel cover (optional)

## 4. Config (`config.json`)
```json
{
  "source": "accounts",          // feed | reels | accounts | explore
  "accounts_file": "accounts.txt", // used when source=accounts
  "max_reels_per_run": 1,
  "delay_min": 5,
  "delay_max": 15,
  "caption_mode": "original",    // original | custom
  "custom_hashtags": [],
  "enable_dedup": true,          // false = allow repeats
  "dedup_file": "data/uploaded.db",
  "cover_path": "cover",         // file or folder (random from cover/ if folder)
  "log_file": "data/logs/bot.log",
  "log_level": "INFO",
  "min_likes": 50000,            // filter: like_count >= 50k (0 = disabled)
  "min_comments": 1000,           // comment_count >= 1k
  "min_shares": 500,              // reshare_count >= 500
  "min_views": 100000,            // play_count >= 100k (reels views)
  "max_age_hours": 24             // taken_at within 24h (today)
}
```
Filters are **AND** — reel must pass all set filters (0 = disabled). Checks `like_count`, `comment_count`/`preview_comments`, `reshare_count`/`share_count`, `play_count`/`view_count`, `taken_at`.

**Source modes:**
- `feed` — your home timeline (personalized, may be ad-heavy → auto fallback to `clips/discover`)
- `reels` / `explore_reels` — reels discovery (clips/discover, personalized)
- `accounts` — picks **random account** from `accounts.txt`, then **random video** from that account (same flow after)

**accounts.txt**
```
@instagram
@natgeo
@nike
# one per line, @ optional, # comments ignored
@your_niche_account
```

## 5. Endpoints (when `python app.py` running)
- `GET /` / `GET /health` → `200 ok` (Render healthcheck)
- `GET /upload` → triggers `main.py` in background, returns HTML with live log via `EventSource('/logs/stream')`
  - `GET /upload?stream=1` → raw `text/event-stream` SSE
  - `POST /upload` also works
- `GET /logs` → last 200 lines of `data/logs/bot.log`
- `GET /logs/stream` → SSE live tail

**Trigger upload (without recommit) — query overrides:**
```bash
# use feed (your timeline)
curl https://instaup-ayp2.onrender.com/upload?src=feed

# use reels discovery
curl https://instaup-ayp2.onrender.com/upload?src=reels

# use accounts - random account from list (no recommit)
curl "https://instaup-ayp2.onrender.com/upload?src=accounts&account=@natgeo,@nike"
curl "https://instaup-ayp2.onrender.com/upload?src=accounts&account=@instagram&account=@nike"
# also supports: ?account=@xyz (single), ?account=@xyz,@abcd (comma), ?accounts=@a,@b

# custom cover per request
curl "https://instaup-ayp2.onrender.com/upload?cover=cover/2.jpg"
curl "https://instaup-ayp2.onrender.com/upload?cover=cover"  # random from cover/

# other overrides (any config.json key)
curl "https://instaup-ayp2.onrender.com/upload?caption_mode=custom&custom_hashtags=viral,trending"
curl "https://instaup-ayp2.onrender.com/upload?enable_dedup=false"
curl "https://instaup-ayp2.onrender.com/upload?src=accounts&account=@your_niche&cover=cover/3.png&enable_dedup=true"

# engagement filters (upload only if passes)
curl "https://instaup-ayp2.onrender.com/upload?src=feed&min_likes=50000&max_age_hours=24"
curl "https://instaup-ayp2.onrender.com/upload?src=accounts&account=@natgeo&min_likes=50000&min_comments=1000&min_shares=500&min_views=100000&max_age_hours=24"
curl "https://instaup-ayp2.onrender.com/upload?min_likes=100000&min_comments=5000"  # any source

# stream live log
curl -N "https://instaup-ayp2.onrender.com/upload?src=accounts&account=@natgeo&stream=1"

# keep alive
curl https://instaup-ayp2.onrender.com/health
```
All query keys override `config.json` without `git push` — `src` alias for `source`, `account` alias for `accounts`, `cover` alias for `cover_path`.

Keep free service from sleeping: set **UptimeRobot** to hit `/health` every 5 min and hit `/upload` on schedule.

## 6. Logs & Storage (Render free ephemeral)
- `data/uploaded.db` (SQLite WAL, dedup) — survives deploys if in repo, but Render disk is ephemeral → commit periodically or use external DB for persistence
- `data/tmp/` auto-cleaned after each upload
- `data/logs/bot.log` Rotating 1 MB

## 7. Troubleshooting
- **Build 5 min → fail:** ensure `runtime.txt: python-3.11.11` + `build.sh` (--prefer-binary, --no-deps for Pillow 12.2 vs moviepy<12). Clear build cache & redeploy.
- **Permission denied /mnt/sdcard:** fixed `dedup.py` now uses `Path(__file__).parent.parent` (portable)
- **Pillow / pydantic / pycryptodomex conflicts:** `build.sh` handles `Pillow 12.2.0` vs `moviepy` via `--no-deps`
- **Upload shows only `=== Upload job started ===`:** wait 60-90s (feed 20s + clips 5s + download 1s + upload 30s), keep `/upload` page open — EventSource streams live

## 8. Local Test
```bash
pip install -r requirements.txt  # or bash build.sh
python main.py
# or with custom config
python main.py --config config.json
# web
python app.py  # then curl localhost:8080/health
```
