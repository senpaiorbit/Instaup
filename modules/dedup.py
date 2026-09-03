import json
import sqlite3
from pathlib import Path

ROOT = Path("/mnt/sdcard/htdocs/instabot")

# Connection cache only (no full-set cache to avoid OOM on 10k+ ids)
_conns: dict[str, sqlite3.Connection] = {}


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


def _get_conn(path: Path) -> sqlite3.Connection:
    key = str(path)
    if key in _conns:
        return _conns[key]
    conn = sqlite3.connect(key, check_same_thread=False, timeout=10)
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
    """Return full set of uploaded media_ids. Avoid calling on large DBs."""
    p = _resolve_path(path)
    conn = _get_conn(p)
    cur = conn.execute("SELECT media_id FROM uploaded;")
    return {row[0] for row in cur}


def load_uploaded_lazy(path: str | None = None):
    """Yield media_ids one at a time (low memory for huge DBs)."""
    p = _resolve_path(path)
    conn = _get_conn(p)
    cur = conn.execute("SELECT media_id FROM uploaded;")
    for row in cur:
        yield row[0]


def is_uploaded(media_id: str | int, path: str | None = None) -> bool:
    p = _resolve_path(path)
    mid = str(media_id)
    conn = _get_conn(p)
    cur = conn.execute(
        "SELECT 1 FROM uploaded WHERE media_id=? LIMIT 1;", (mid,)
    )
    return cur.fetchone() is not None


def mark_uploaded(media_id: str | int, path: str | None = None) -> None:
    p = _resolve_path(path)
    mid = str(media_id)
    conn = _get_conn(p)
    conn.execute(
        "INSERT OR IGNORE INTO uploaded (media_id) VALUES (?);", (mid,)
    )


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
    existing = {row[0] for row in cur}
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
        return
    p = _resolve_path(path)
    key = str(p)
    conn = _conns.pop(key, None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
