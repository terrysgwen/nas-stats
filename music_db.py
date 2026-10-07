"""SQLite database layer for Music Player.

Stores playlist, fnOS music cache and settings in a separate database (data/music.db).
NEVER touches or modifies data/nas-stats.db.
"""

import sqlite3
import threading
from pathlib import Path
import uuid
from typing import List, Dict, Any, Optional

DB_PATH = Path(__file__).parent / "data" / "music.db"
_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """Get a thread-local database connection for music."""
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


DEFAULT_PLAYLIST = [
    {
        "id": "preset_1",
        "title": "SoundHelix Ambient Chill",
        "artist": "SoundHelix",
        "album": "Electronic Relaxation",
        "url": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
        "cover": "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=300&q=80",
        "sort_order": 1
    },
    {
        "id": "preset_2",
        "title": "Acoustic Breeze",
        "artist": "Benjamin Tissot",
        "album": "Acoustic Moments",
        "url": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
        "cover": "https://images.unsplash.com/photo-1470225620780-dba8ba36b745?w=300&q=80",
        "sort_order": 2
    },
    {
        "id": "preset_3",
        "title": "Summer Piano Melodies",
        "artist": "SoundHelix Piano",
        "album": "Piano Harmonies",
        "url": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3",
        "cover": "https://images.unsplash.com/photo-1514525253161-7a46d19cd819?w=300&q=80",
        "sort_order": 3
    }
]


def init_music_db():
    """Initialize music.db tables and seed default playlist if empty."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS playlist (
            id         TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            artist     TEXT DEFAULT '未知歌手',
            album      TEXT DEFAULT '默认专辑',
            url        TEXT NOT NULL,
            cover      TEXT DEFAULT '',
            source     TEXT DEFAULT 'custom',
            guid       TEXT DEFAULT '',
            duration   INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS music_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()

    # Migration: check and add missing columns if upgrading existing music.db
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(playlist)").fetchall()]
    if "source" not in cols:
        conn.execute("ALTER TABLE playlist ADD COLUMN source TEXT DEFAULT 'custom'")
    if "guid" not in cols:
        conn.execute("ALTER TABLE playlist ADD COLUMN guid TEXT DEFAULT ''")
    if "duration" not in cols:
        conn.execute("ALTER TABLE playlist ADD COLUMN duration INTEGER DEFAULT 0")
    conn.commit()


def get_playlist() -> List[Dict[str, Any]]:
    """Retrieve full playlist ordered by sort_order."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, title, artist, album, url, cover, source, guid, duration, sort_order 
        FROM playlist 
        ORDER BY sort_order ASC, created_at ASC
    """).fetchall()
    return [dict(r) for r in rows]


def add_song(title: str, artist: str = "", album: str = "", url: str = "", cover: str = "", source: str = "custom", guid: str = "", duration: int = 0) -> str:
    """Add a new song to the playlist."""
    conn = get_conn()
    song_id = f"fn_{guid}" if guid else f"song_{str(uuid.uuid4())[:8]}"
    max_order = conn.execute("SELECT MAX(sort_order) as m FROM playlist").fetchone()["m"] or 0
    conn.execute("""
        INSERT OR REPLACE INTO playlist (id, title, artist, album, url, cover, source, guid, duration, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (song_id, title, artist or "未知歌手", album or "飞牛音乐", url, cover or "", source, guid, duration, max_order + 1))
    conn.commit()
    return song_id


def delete_song(song_id: str):
    """Delete a song by id."""
    conn = get_conn()
    conn.execute("DELETE FROM playlist WHERE id = ?", (song_id,))
    conn.commit()


def reset_playlist():
    """Reset playlist back to default tracks."""
    conn = get_conn()
    conn.execute("DELETE FROM playlist")
    for item in DEFAULT_PLAYLIST:
        conn.execute("""
            INSERT INTO playlist (id, title, artist, album, url, cover, source, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, 'preset', ?)
        """, (item["id"], item["title"], item["artist"], item["album"], item["url"], item["cover"], item["sort_order"]))
    conn.commit()


def sync_fnos_tracks(tracks: List[Dict[str, Any]], replace: bool = False):
    """Sync tracks fetched from fnOS Music API into local playlist table."""
    conn = get_conn()
    if replace:
        conn.execute("DELETE FROM playlist")
    
    max_order = conn.execute("SELECT MAX(sort_order) as m FROM playlist").fetchone()["m"] or 0
    for idx, t in enumerate(tracks):
        guid = t.get("guid") or t.get("id") or ""
        if not guid:
            continue
        song_id = f"fn_{guid}"
        title = t.get("title") or t.get("name") or "未知曲目"
        artists_list = t.get("artists") or []
        if t.get("artist"):
            artist = t.get("artist")
        elif artists_list and isinstance(artists_list, list):
            artist = ", ".join([a.get("name", "") for a in artists_list if a.get("name")]) or "未知歌手"
        else:
            artist = "未知歌手"

        album = t.get("album", {}).get("name", "") if isinstance(t.get("album"), dict) else (t.get("album") or "")
        cover_id = t.get("coverId") or ""
        dur_raw = t.get("duration") or 0
        duration = dur_raw // 1000 if dur_raw > 1000 else dur_raw
        
        # Audio stream and cover URLs proxy through nas-stats
        stream_url = f"/api/music/fnos/stream?guid={guid}"
        cover_url = f"/api/music/fnos/cover?coverId={cover_id}" if cover_id else ""
        
        conn.execute("""
            INSERT OR REPLACE INTO playlist (id, title, artist, album, url, cover, source, guid, duration, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, 'fnos', ?, ?, ?)
        """, (song_id, title, artist, album or "飞牛音乐", stream_url, cover_url, guid, duration, max_order + idx + 1))
    conn.commit()


def get_music_config(key: str, default: Any = None) -> Any:
    conn = get_conn()
    row = conn.execute("SELECT value FROM music_config WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    return default


def set_music_config(key: str, value: str):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO music_config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
