import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_log = logging.getLogger("instabot.dedup")

# Connection cache only (no full-set cache to avoid OOM on 10k+ ids)
_conns: dict[str, sqlite3.Connection] = {}

# In-memory ids marked during THIS process lifetime (bounded: only marks made
# here). Merged into every read so an always-on service is never blind to its
# own writes, and a long-lived process keeps dedup working even if the disk
# is wiped mid-run (Render free ephemeral disk).
_memory: dict[str, set[str]] = {}

# Guard so backup seeding / fresh-deploy logging happens once per process.
_seeded: dict[str, bool] = {}

# (mtime_ns, size) of last git-synced backup, to avoid needless commits.
_last_sync: dict[str, tuple] = {}

BACKUP_SUFFIX = "_backup.json"


def _resolve_path(path: str | None = None) -> Path:
    if path is None:
        path = "data/uploaded.db"
    if isinstance(path, str) and path.endswith(".json"):
        path = path[:-5] + ".db"
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _backup_path(p: Path) -> Path:
    # data/uploaded.db -> data/uploaded_backup.json
    # (NOT gitignored, so it can be committed to the repo and survive deploys)
    return p.with_name(p.stem + BACKUP_SUFFIX)


def _mem(p: Path) -> set:
    key = str(p)
    if key not in _memory:
        _memory[key] = set()
    return _memory[key]


def _load_backup(bp: Path) -> list:
    """Read ids from the backup file (list or dict form). Empty on any error."""
    try:
        with open(bp, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return [str(k) for k in data.keys()]
        if isinstance(data, list):
            return [str(x) for x in data if x is not None]
    except Exception:
        pass
    return []


def _seed_from_backup(conn: sqlite3.Connection, p: Path) -> None:
    """If the DB is empty (new ephemeral deploy), restore ids from the backup
    file so previously uploaded reels are not re-uploaded."""
    key = str(p)
    if _seeded.get(key):
        return
    _seeded[key] = True
    cnt = conn.execute("SELECT COUNT(*) FROM uploaded;").fetchone()[0]
    if cnt > 0:
        return
    bp = _backup_path(p)
    ids = _load_backup(bp)
    if ids:
        conn.executemany(
            "INSERT OR IGNORE INTO uploaded (media_id) VALUES (?);",
            [(x,) for x in ids],
        )
        _log.info(
            "Dedup DB was empty (fresh/ephemeral deploy?): restored %d ids from %s",
            len(ids),
            bp,
        )
    else:
        _log.warning(
            "Dedup DB is empty and no backup found at %s - treating as a new "
            "deploy; previously uploaded reels may be re-uploaded.",
            bp,
        )


def _git_sync(bp: Path) -> None:
    """Opt-in persistence across deploys: commit+push the backup file.
    Enable by setting env DEDUP_GIT_SYNC=true (repo must have a git remote
    with push credentials). Never blocks or raises on failure."""
    if os.environ.get("DEDUP_GIT_SYNC", "").lower() != "true":
        return
    try:
        st = bp.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return
    if _last_sync.get(str(bp)) == sig:
        return
    if not (ROOT / ".git").is_dir():
        return
    try:
        subprocess.run(
            ["git", "add", "--", str(bp)],
            cwd=ROOT, capture_output=True, check=False,
        )
        res = subprocess.run(
            ["git", "commit", "-m", "chore(dedup): persist uploaded ids backup"],
            cwd=ROOT, capture_output=True, check=False,
        )
        if res.returncode == 0:
            push = subprocess.run(
                ["git", "push"], cwd=ROOT,
                capture_output=True, check=False,
            )
            if push.returncode != 0:
                _log.warning(
                    "Dedup backup committed but git push failed: %s",
                    push.stderr.decode(errors="replace")[-300:],
                )
                return
            _log.info("Dedup backup committed & pushed (%s)", bp)
    except Exception as e:
        _log.debug("Dedup git sync skipped: %s", e)
        return
    _last_sync[str(bp)] = sig


def _persist_backup(conn: sqlite3.Connection, p: Path) -> None:
    """Atomically rewrite the committable backup file with the full id set
    (DB + this process's in-memory marks)."""
    bp = _backup_path(p)
    ids = set(_mem(p))
    for row in conn.execute("SELECT media_id FROM uploaded;"):
        ids.add(row[0])
    tmp = Path(str(bp) + ".tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f)
        os.replace(tmp, bp)
    except Exception as e:
        _log.warning("Dedup backup write failed for %s: %s", bp, e)
        return
    _git_sync(bp)


def _get_conn(path: Path) -> sqlite3.Connection:
    key = str(path)
    if key in _conns:
        return _conns[key]
    conn = sqlite3.connect(key, check_same_thread=False, timeout=10)
    # autocommit: every write is durable immediately, so cron<-web and
    # process-to-process reads always see marks (important on shared/ephemeral fs)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-8192;")  # 8 MB page cache
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS uploaded (media_id TEXT PRIMARY KEY) WITHOUT ROWID;"
    )
    # migrate old json into db if needed (stream, don't hold full list)
    if key.endswith(".db"):
        json_path = Path(key[:-3] + ".json")
        if json_path.exists():
            try:
                cnt = conn.execute("SELECT COUNT(*) FROM uploaded;").fetchone()[0]
                if cnt == 0:
                    with open(json_path, "r") as f:
                        data = json.load(f)
                    if isinstance(data, list) and data:
                        conn.executemany(
                            "INSERT OR IGNORE INTO uploaded (media_id) VALUES (?);",
                            [(str(x),) for x in data],
                        )
            except Exception:
                pass
    _seed_from_backup(conn, path)
    _conns[key] = conn
    return conn


def _get_media_id(media) -> str | None:
    if isinstance(media, dict):
        v = media.get("id") or media.get("pk")
        return str(v) if v else None
    for attr in ("id", "pk"):
        val = getattr(media, attr, None)
        if val is not None:
            return str(val)
    return None


def load_uploaded(path: str | None = None) -> set:
    """Return full set of uploaded media_ids (DB + in-memory). Avoid on huge DBs."""
    p = _resolve_path(path)
    conn = _get_conn(p)
    cur = conn.execute("SELECT media_id FROM uploaded;")
    return {row[0] for row in cur} | _mem(p)


def load_uploaded_lazy(path: str | None = None):
    """Yield media_ids one at a time (low memory for huge DBs)."""
    p = _resolve_path(path)
    conn = _get_conn(p)
    cur = conn.execute("SELECT media_id FROM uploaded;")
    seen = set()
    for row in cur:
        seen.add(row[0])
        yield row[0]
    for mid in _mem(p):
        if mid not in seen:
            yield mid


def is_uploaded(media_id: str | int, path: str | None = None) -> bool:
    p = _resolve_path(path)
    mid = str(media_id)
    if mid in _mem(p):
        return True
    conn = _get_conn(p)
    cur = conn.execute(
        "SELECT 1 FROM uploaded WHERE media_id=? LIMIT 1;", (mid,)
    )
    return cur.fetchone() is not None


def mark_uploaded(media_id: str | int, path: str | None = None) -> None:
    p = _resolve_path(path)
    mid = str(media_id)
    conn = _get_conn(p)
    _mem(p).add(mid)  # in-memory first: visible even if disk write later fails
    conn.execute(
        "INSERT OR IGNORE INTO uploaded (media_id) VALUES (?);", (mid,)
    )
    _persist_backup(conn, p)


def filter_new(medias: list, path: str | None = None, logger=None) -> list:
    """Return only medias whose ids are NOT in the DB. No full-set load."""
    p = _resolve_path(path)
    ids = []
    id_to_media: dict[str, object] = {}
    for m in medias:
        mid = _get_media_id(m)
        if mid:
            ids.append(mid)
            id_to_media[mid] = m
    if not ids:
        return medias
    conn = _get_conn(p)
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"SELECT media_id FROM uploaded WHERE media_id IN ({placeholders});",
        ids,
    )
    existing = {row[0] for row in cur} | _mem(p)
    new = []
    skipped = 0
    for mid in ids:
        if mid in existing:
            skipped += 1
        else:
            new.append(id_to_media[mid])
    for m in medias:
        if not _get_media_id(m):
            new.append(m)
    if logger and skipped:
        logger.info(
            f"Dedup: skipped {skipped} already uploaded, "
            f"{len(new)} new (DB query, no full cache)"
        )
    return new


def close(path: str | None = None) -> None:
    """Explicitly close connection for a path (useful on ephemeral FS)."""
    if path is None:
        for k in list(_conns):
            try:
                _conns[k].close()
            except Exception:
                pass
        _conns.clear()
        _memory.clear()
        _seeded.clear()
        return
    p = _resolve_path(path)
    key = str(p)
    conn = _conns.pop(key, None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
    _memory.pop(key, None)
    _seeded.pop(key, None)