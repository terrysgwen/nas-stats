"""SQLite database layer for NAS Stats.

Replaces config.json and services.json with a single SQLite database.
Thread-safe via thread-local connections.
"""

import json
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "nas-stats.db"

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """Get a thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS monitors (
            id             TEXT PRIMARY KEY,
            display_name   TEXT NOT NULL,
            enabled        INTEGER DEFAULT 0,
            show_in_banner INTEGER DEFAULT 1,
            config         TEXT DEFAULT '{}',
            sort_order     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS services (
            id          TEXT PRIMARY KEY,
            port        INTEGER,
            name        TEXT,
            category    TEXT    DEFAULT '自定义',
            protocol    TEXT    DEFAULT 'http',
            icon        TEXT    DEFAULT '🔧',
            description TEXT    DEFAULT '',
            url         TEXT    DEFAULT '',
            favicon     TEXT    DEFAULT '',
            visible     INTEGER DEFAULT 1,
            sort_order  INTEGER DEFAULT 0
        );
    """)
    conn.commit()


# ── Config (key-value) ──────────────────────────────────────────────

def get_config(key: str, default=None):
    row = get_conn().execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    if row:
        val = row["value"]
        try:
            return json.loads(val)
        except Exception:
            return val
    return default


def set_config(key: str, value):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()


def get_all_config() -> dict:
    rows = get_conn().execute("SELECT key, value FROM config").fetchall()
    res = {}
    for r in rows:
        val = r["value"]
        try:
            res[r["key"]] = json.loads(val)
        except Exception:
            res[r["key"]] = val
    return res


# ── Monitors ────────────────────────────────────────────────────────

def get_all_monitors() -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM monitors ORDER BY sort_order, id"
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["config"] = json.loads(d["config"]) if d["config"] else {}
        d["enabled"] = bool(d["enabled"])
        d["show_in_banner"] = bool(d["show_in_banner"])
        result.append(d)
    return result


def get_monitor(monitor_id: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM monitors WHERE id=?", (monitor_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["config"] = json.loads(d["config"]) if d["config"] else {}
    d["enabled"] = bool(d["enabled"])
    d["show_in_banner"] = bool(d["show_in_banner"])
    return d


def upsert_monitor(
    monitor_id: str,
    display_name: str,
    enabled: bool = False,
    show_in_banner: bool = True,
    config: dict | None = None,
    sort_order: int = 0,
):
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO monitors
           (id, display_name, enabled, show_in_banner, config, sort_order)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            display_name,
            int(enabled),
            int(show_in_banner),
            json.dumps(config or {}, ensure_ascii=False),
            sort_order,
        ),
    )
    conn.commit()


def update_monitor(monitor_id: str, **kwargs):
    conn = get_conn()
    sets, vals = [], []
    for k, v in kwargs.items():
        if k == "config":
            sets.append("config=?")
            vals.append(json.dumps(v, ensure_ascii=False))
        elif k in ("enabled", "show_in_banner"):
            sets.append(f"{k}=?")
            vals.append(int(v))
        elif k in ("display_name", "sort_order"):
            sets.append(f"{k}=?")
            vals.append(v)
    if sets:
        vals.append(monitor_id)
        conn.execute(
            f"UPDATE monitors SET {', '.join(sets)} WHERE id=?", vals
        )
        conn.commit()


def get_enabled_monitor_ids() -> list[str]:
    rows = get_conn().execute(
        "SELECT id FROM monitors WHERE enabled=1 ORDER BY sort_order"
    ).fetchall()
    return [r["id"] for r in rows]


# ── Services ────────────────────────────────────────────────────────

def get_all_services() -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM services ORDER BY sort_order"
    ).fetchall()
    return [dict(r) for r in rows]


def add_service(svc: dict):
    conn = get_conn()
    data = {
        "id": svc.get("id"),
        "port": svc.get("port"),
        "name": svc.get("name"),
        "category": svc.get("category", "自定义"),
        "protocol": svc.get("protocol", "http"),
        "icon": svc.get("icon", "🔧"),
        "description": svc.get("description", ""),
        "url": svc.get("url", ""),
        "favicon": svc.get("favicon", ""),
        "visible": svc.get("visible", 1),
        "sort_order": svc.get("sort_order", 0),
    }
    conn.execute(
        """INSERT INTO services
           (id, port, name, category, protocol, icon, description, url, favicon, visible, sort_order)
           VALUES (:id, :port, :name, :category, :protocol, :icon, :description, :url, :favicon, :visible, :sort_order)""",
        data,
    )
    conn.commit()


def update_service(service_id: str, **kwargs):
    conn = get_conn()
    sets, vals = [], []
    for k, v in kwargs.items():
        sets.append(f"{k}=?")
        vals.append(v)
    if sets:
        vals.append(service_id)
        conn.execute(
            f"UPDATE services SET {', '.join(sets)} WHERE id=?", vals
        )
        conn.commit()


def delete_service(service_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM services WHERE id=?", (service_id,))
    conn.commit()


def delete_services_batch(service_ids: list[str]):
    if not service_ids:
        return
    conn = get_conn()
    placeholders = ",".join("?" for _ in service_ids)
    conn.execute(f"DELETE FROM services WHERE id IN ({placeholders})", service_ids)
    conn.commit()


def get_service_ports() -> set[int]:
    rows = get_conn().execute("SELECT port FROM services").fetchall()
    return {r["port"] for r in rows}


def get_service_port_protocol_pairs() -> set[tuple]:
    rows = get_conn().execute(
        "SELECT port, protocol FROM services"
    ).fetchall()
    return {(r["port"], r["protocol"]) for r in rows}


def count_services() -> int:
    row = get_conn().execute("SELECT COUNT(*) AS c FROM services").fetchone()
    return row["c"]


def reorder_services(order_map: dict[str, int]):
    conn = get_conn()
    for sid, order in order_map.items():
        conn.execute(
            "UPDATE services SET sort_order=? WHERE id=?", (order, sid)
        )
    conn.commit()


# ── Migration ───────────────────────────────────────────────────────

LEGACY_MONITOR_DEFS = {
    "qbittorrent": ("qBittorrent", ["url", "username", "password"]),
    "transmission": ("Transmission", ["url", "username", "password"]),
    "ikuai": ("爱快路由器", ["url", "username", "password"]),
    "pve": ("Proxmox VE", ["url", "username", "password", "token_id", "token_secret", "node"]),
    "moviepilot": ("MoviePilot", ["url", "api_key"]),
}

NEW_MONITOR_DEFS = [
    ("istoreos", "iStoreOS", {"url": "", "username": "root", "password": ""}),
    ("emby", "Emby", {"url": "", "api_key": ""}),
    ("plex", "Plex", {"url": "", "token": ""}),
    ("jellyfin", "Jellyfin", {"url": "", "api_key": ""}),
    ("homeassistant", "Home Assistant", {"url": "", "token": ""}),
    ("easynvr", "EasyNVR", {"url": "", "username": "admin", "password": "admin"}),
]


def migrate_from_legacy(config_path: Path, services_path: Path):
    """Migrate data from config.json + services.json into SQLite on first run."""
    if get_config("_migrated"):
        return

    # ── config.json ──
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            old = json.load(f)

        set_config("port", old.get("port", 8980))
        set_config("nas_ip", old.get("nas_ip", "192.168.1.177"))
        set_config("cors_origins", old.get("cors_origins", ["*"]))
        set_config("ui", old.get("ui", {"show_search": True, "theme": "dark"}))

        for i, (mid, (name, fields)) in enumerate(LEGACY_MONITOR_DEFS.items()):
            svc = old.get("services", {}).get(mid, {})
            cfg = {f: svc.get(f, "") for f in fields}
            upsert_monitor(
                mid, name,
                enabled=svc.get("enabled", False),
                config=cfg,
                sort_order=i,
            )

    # ── new monitors (disabled by default) ──
    for i, (mid, name, default_cfg) in enumerate(NEW_MONITOR_DEFS):
        if not get_monitor(mid):
            upsert_monitor(mid, name, enabled=False, config=default_cfg, sort_order=10 + i)

    # ── services.json ──
    if services_path.exists():
        with open(services_path, "r", encoding="utf-8") as f:
            old_services = json.load(f)
        for svc in old_services:
            try:
                add_service({
                    "id": svc["id"],
                    "port": svc.get("port", 0),
                    "name": svc.get("name", ""),
                    "category": svc.get("category", "自定义"),
                    "protocol": svc.get("protocol", "http"),
                    "icon": svc.get("icon", "🔧"),
                    "description": svc.get("description", ""),
                    "url": svc.get("url", ""),
                    "favicon": svc.get("favicon", ""),
                    "visible": int(svc.get("visible", True)),
                    "sort_order": svc.get("sort_order", 0),
                })
            except sqlite3.IntegrityError:
                pass

    set_config("_migrated", True)


def seed_default_monitors():
    """Ensure all monitor types exist in DB (for fresh installs)."""
    for i, (mid, (name, _fields)) in enumerate(LEGACY_MONITOR_DEFS.items()):
        if not get_monitor(mid):
            upsert_monitor(mid, name, enabled=False, config={}, sort_order=i)
    for i, (mid, name, default_cfg) in enumerate(NEW_MONITOR_DEFS):
        if not get_monitor(mid):
            upsert_monitor(mid, name, enabled=False, config=default_cfg, sort_order=10 + i)
