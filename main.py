"""FastAPI backend application for NAS Stats 2.0.

All configuration and services are persisted in SQLite (database.py).
Monitors are dynamically discovered and instantiated from monitors package.
"""

import asyncio
import base64
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urljoin, urlparse
import html as html_lib
import ipaddress
import logging
import uuid
from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
import httpx

logger = logging.getLogger("main")


def validate_proxy_url(url: str, allow_private: bool = True) -> str:
    """Validate proxy destination URL against SSRF and illegal schemes."""
    if not url or not isinstance(url, str):
        raise HTTPException(status_code=400, detail="缺少有效的 URL")
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="只允许 HTTP 或 HTTPS 协议链接")

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(status_code=400, detail="无法解析的目标主机")

    # Always block cloud metadata services
    if hostname in ("169.254.169.254", "metadata.google.internal"):
        raise HTTPException(status_code=403, detail="禁止请求云服务元数据端点")

    # Check IP restrictions if hostname is an IP
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_multicast or ip.is_reserved:
            raise HTTPException(status_code=403, detail="非法的目标 IP 地址")
        if not allow_private and (ip.is_private or ip.is_loopback or ip.is_link_local):
            raise HTTPException(status_code=403, detail="禁止向私有/回环网络发起外链代理请求")
    except ValueError:
        # Not a raw IP literal (e.g. domain name)
        pass

    return url

from auth_manager import auth_mgr


async def require_auth(request: Request):
    """Dependency that requires valid authentication."""
    if not auth_mgr.is_enabled():
        return {"username": "admin", "auth_disabled": True}

    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "x-auth-token" in request.headers:
        token = request.headers["x-auth-token"].strip()

    if not token:
        raise HTTPException(status_code=401, detail="未授权，请先登录管理面板")

    data = auth_mgr.validate_token(token)
    if not data:
        raise HTTPException(status_code=401, detail="登录凭证已失效或过期，请重新登录")

    return data


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, taking proxy headers into account."""
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


import database as db
from monitors import MONITOR_REGISTRY, BaseMonitor

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_LEGACY_PATH = BASE_DIR / "data" / "config.legacy.json"
SERVICES_LEGACY_PATH = BASE_DIR / "data" / "services.legacy.json"

# Initialize DB and run migration if needed
db.init_db()
db.migrate_from_legacy(CONFIG_LEGACY_PATH, SERVICES_LEGACY_PATH)
db.seed_default_monitors()

# Auto-seed default service cards if services table is empty (fresh deploy)
if db.count_services() == 0:
    try:
        from main_defaults import DEFAULT_SERVICES
        for s in DEFAULT_SERVICES:
            db.add_service({
                "id": f"svc_{s['port']}",
                "port": s["port"],
                "name": s["name"],
                "category": s.get("category", "自定义"),
                "protocol": s.get("protocol", "http"),
                "icon": s.get("icon", "🔧"),
                "description": s.get("description", ""),
                "url": s.get("url", ""),
                "visible": 1,
                "sort_order": s.get("sort_order", 0),
            })
    except Exception as e:
        logger.warning(f"Auto-seed default services failed: {e}")

client: httpx.AsyncClient | None = None
active_monitors: dict[str, BaseMonitor] = {}
stats_cache = {"data": None, "ts": 0}
CACHE_TTL = 3


def reload_monitors():
    """Instantiate or refresh monitors according to database state."""
    global active_monitors, client
    db_monitors = db.get_all_monitors()
    current_instances = {}

    for m_data in db_monitors:
        mid = m_data["id"]
        if mid not in MONITOR_REGISTRY:
            continue

        cls = MONITOR_REGISTRY[mid]
        # Preserve instance if already exists to keep connection/cookies
        if mid in active_monitors:
            inst = active_monitors[mid]
            inst.update_config(m_data["config"])
        else:
            inst = cls(mid, m_data["config"])
            if client:
                inst.set_client(client)
        current_instances[mid] = inst

    active_monitors = current_instances


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=10, follow_redirects=True, verify=False)
    for m in active_monitors.values():
        m.set_client(client)
    reload_monitors()
    yield
    if client and not client.is_closed:
        await client.aclose()


def get_http_client(timeout: float = 10.0) -> httpx.AsyncClient:
    global client
    if client and not client.is_closed:
        return client
    return httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=True)


app = FastAPI(title="NAS Stats 2.3 API", lifespan=lifespan)

@app.middleware("http")
async def add_no_cache_for_html(request: Request, call_next):
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if "text/html" in ct:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Setup CORS from DB config (default to empty list to block unauthorized cross-origin requests)
cors_origins = db.get_config("cors_origins", [])
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ── Pages ──────────────────────────────────────────────────────────

@app.get("/")
@app.get("/index.html")
async def index():
    resp = FileResponse(STATIC_DIR / "index.html", media_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/nvr")
async def nvr_page():
    resp = FileResponse(STATIC_DIR / "nvr.html", media_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ── Health & Stats ──────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.3.0"}


@app.get("/api/stats")
async def get_stats():
    now = time.time()
    if stats_cache["data"] and now - stats_cache["ts"] < CACHE_TTL:
        return stats_cache["data"]

    db_monitors = {m["id"]: m for m in db.get_all_monitors()}
    tasks = []
    keys = []

    for mid, inst in active_monitors.items():
        m_info = db_monitors.get(mid)
        if not m_info or not m_info["enabled"]:
            continue
        keys.append(mid)
        tasks.append(inst.fetch())

    results = {}
    if tasks:
        task_results = await asyncio.gather(*tasks, return_exceptions=True)
        for mid, res in zip(keys, task_results):
            if isinstance(res, Exception):
                results[mid] = {"enabled": True, "status": "error", "error": str(res)}
            else:
                results[mid] = res

    # Keep backward compatibility and attach show_in_banner metadata
    final_output = {}
    for mid, m_info in db_monitors.items():
        if m_info["enabled"] and mid in results:
            data = dict(results[mid])
            data["show_in_banner"] = m_info["show_in_banner"]
            data["display_name"] = m_info["display_name"]
            final_output[mid] = data
        else:
            final_output[mid] = {
                "enabled": False,
                "status": "disabled",
                "show_in_banner": m_info["show_in_banner"],
                "display_name": m_info["display_name"]
            }

    final_output["timestamp"] = int(now)
    stats_cache["data"] = final_output
    stats_cache["ts"] = now
    return final_output


# ── Monitor Management ──────────────────────────────────────────────

@app.get("/api/monitors")
async def list_monitors():
    monitors = db.get_all_monitors()
    # Mask secret fields
    safe_list = []
    for m in monitors:
        cfg = dict(m["config"])
        for secret_key in ("password", "token_secret", "token", "api_key"):
            if secret_key in cfg and cfg[secret_key]:
                cfg[secret_key] = "********"
        safe_list.append({
            "id": m["id"],
            "display_name": m["display_name"],
            "enabled": m["enabled"],
            "show_in_banner": m["show_in_banner"],
            "sort_order": m["sort_order"],
            "config": cfg,
        })
    return {"items": safe_list}


@app.post("/api/monitors/{monitor_id}")
async def update_monitor_endpoint(monitor_id: str, request: Request, _auth: dict = Depends(require_auth)):
    body = await request.json()
    existing = db.get_monitor(monitor_id)
    if not existing:
        return JSONResponse({"error": "Monitor not found"}, status_code=404)

    updates = {}
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])
    if "show_in_banner" in body:
        updates["show_in_banner"] = bool(body["show_in_banner"])
    if "display_name" in body:
        updates["display_name"] = str(body["display_name"])
    if "sort_order" in body:
        updates["sort_order"] = int(body["sort_order"])

    if "config" in body and isinstance(body["config"], dict):
        current_cfg = existing["config"]
        new_cfg = dict(body["config"])
        # Retain old passwords if masked
        for secret_key in ("password", "token_secret", "token", "api_key"):
            if new_cfg.get(secret_key) in ("********", "", None) and current_cfg.get(secret_key):
                new_cfg[secret_key] = current_cfg[secret_key]
        updates["config"] = new_cfg

    db.update_monitor(monitor_id, **updates)
    reload_monitors()
    stats_cache["data"] = None  # invalidate cache
    return {"ok": True}


@app.post("/api/monitors/{monitor_id}/test")
async def test_monitor(monitor_id: str, _auth: dict = Depends(require_auth)):
    if monitor_id not in active_monitors:
        return JSONResponse({"error": "未启用的监控组件"}, status_code=404)
    inst = active_monitors[monitor_id]
    result = await inst.test_connection()
    return result


# ── Config API ──────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config_endpoint():
    ui = db.get_config("ui", {"show_search": True, "theme": "dark", "card_style": "default"})
    port = db.get_config("port", 8980)
    nas_ip = db.get_config("nas_ip", "127.0.0.1")
    return {
        "ui": ui,
        "port": port,
        "nas_ip": nas_ip,
    }


@app.post("/api/config")
async def update_config_endpoint(request: Request, _auth: dict = Depends(require_auth)):
    body = await request.json()
    if "ui" in body and isinstance(body["ui"], dict):
        ui = db.get_config("ui", {})
        ui.update(body["ui"])
        db.set_config("ui", ui)
    if "nas_ip" in body:
        db.set_config("nas_ip", str(body["nas_ip"]))
    if "port" in body:
        db.set_config("port", int(body["port"]))
    return {"ok": True}


# ── Web Authentication API ──────────────────────────────────────────


@app.get("/api/auth/status")
async def auth_status_endpoint():
    """Get web authentication configuration and status."""
    return {"ok": True, **auth_mgr.get_status()}


@app.post("/api/auth/login")
async def auth_login_endpoint(request: Request):
    """Authenticate user credentials and generate persistent token with rate limiting."""
    client_ip = _get_client_ip(request)

    # Check IP lock
    locked, remain_sec = auth_mgr.is_ip_locked(client_ip)
    if locked:
        logger.warning("登录被拦截: IP %s 已被临时封禁，剩余 %d 秒", client_ip, remain_sec)
        return JSONResponse(
            {"ok": False, "error": f"登录失败次数过多，该 IP 已被临时锁定，请在 {remain_sec} 秒后再试"},
            status_code=429
        )

    try:
        body = await request.json()
    except Exception:
        body = {}
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    remember = bool(body.get("remember", True))

    if not auth_mgr.verify_credentials(username, password):
        count, lock_time = auth_mgr.record_login_failure(client_ip)
        logger.warning("登录失败: 用户名 '%s' 来自 IP %s (失败次数: %d)", username, client_ip, count)
        if lock_time > 0:
            return JSONResponse(
                {"ok": False, "error": f"连续 5 次输入错误，该 IP 已被临时锁定 15 分钟"},
                status_code=429
            )
        remain_tries = max(0, 5 - count)
        return JSONResponse(
            {"ok": False, "error": f"用户名或密码错误（还剩 {remain_tries} 次机会）"},
            status_code=401
        )

    # Login successful
    auth_mgr.record_login_success(client_ip)
    token = auth_mgr.create_token(username or "admin", remember=remember)
    logger.info("用户 '%s' 从 IP %s 登录成功", username or "admin", client_ip)
    return {"ok": True, "token": token, "username": username or "admin", "remember": remember}


@app.get("/api/auth/verify")
async def auth_verify_endpoint(request: Request):
    """Verify session token."""
    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "x-auth-token" in request.headers:
        token = request.headers["x-auth-token"].strip()

    data = auth_mgr.validate_token(token)
    if data:
        return {"ok": True, "authenticated": True, "username": data.get("username")}
    return JSONResponse({"ok": False, "authenticated": False}, status_code=401)


@app.post("/api/auth/logout")
async def auth_logout_endpoint(request: Request):
    """Revoke current session token."""
    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "x-auth-token" in request.headers:
        token = request.headers["x-auth-token"].strip()

    if token:
        auth_mgr.revoke_token(token)
    return {"ok": True}


@app.post("/api/auth/password")
async def auth_change_password_endpoint(request: Request, _auth: dict = Depends(require_auth)):
    """Change web dashboard password (requires active login session)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    old_pwd = body.get("old_password", "").strip()
    new_pwd = body.get("new_password", "").strip()
    ok, msg = auth_mgr.change_password(old_pwd, new_pwd)
    if ok:
        logger.info("管理员密码已成功修改")
        return {"ok": True, "message": msg}
    return JSONResponse({"ok": False, "error": msg}, status_code=400)


import music_db
import music_logger
from fnmusic_client import fnmusic_client

music_db.init_music_db()


def load_fnmusic_config():
    """Load host, port, username, password from music_db or defaults and apply to client."""
    nas_ip = db.get_config("nas_ip", "127.0.0.1")
    host = music_db.get_music_config("fnmusic_host", nas_ip)
    port = int(music_db.get_music_config("fnmusic_port", 5888))
    username = music_db.get_music_config("fnmusic_username", "")
    password = music_db.get_music_config("fnmusic_password", "")
    fnmusic_client.update_config(host=host, port=port, username=username, password=password)


load_fnmusic_config()

# ── Music Player API ────────────────────────────────────────────────

@app.get("/api/music/config")
async def get_music_config_endpoint():
    """Get fnOS Music connection settings (password masked)."""
    nas_ip = db.get_config("nas_ip", "127.0.0.1")
    host = music_db.get_music_config("fnmusic_host", nas_ip)
    port = int(music_db.get_music_config("fnmusic_port", 5888))
    username = music_db.get_music_config("fnmusic_username", "")
    password = music_db.get_music_config("fnmusic_password", "")
    return {
        "ok": True,
        "host": host,
        "port": port,
        "username": username,
        "password": "●●●●●●●●" if password else "",
    }


@app.post("/api/music/config")
async def update_music_config_endpoint(request: Request, _auth: dict = Depends(require_auth)):
    """Update fnOS Music connection credentials (stored in data/music.db)."""
    body = await request.json()
    if "host" in body and body["host"]:
        music_db.set_music_config("fnmusic_host", str(body["host"]).strip())
    if "port" in body and body["port"]:
        music_db.set_music_config("fnmusic_port", int(body["port"]))
    if "username" in body and body["username"]:
        music_db.set_music_config("fnmusic_username", str(body["username"]).strip())
    if "password" in body and body["password"]:
        pwd = str(body["password"]).strip()
        if pwd and pwd != "●●●●●●●●":
            music_db.set_music_config("fnmusic_password", pwd)

    music_logger.log_info(f"⚙️ 收到飞牛音乐配置更新请求: host={body.get('host')}, port={body.get('port')}, user={body.get('username')}")
    load_fnmusic_config()
    # Test authentication immediately with new credentials
    login_ok = await fnmusic_client.login()
    fail_msg = fnmusic_client.last_error or "飞牛音乐配置已保存，但登录验证失败，请检查账号密码或 5888 端口。"
    if login_ok:
        music_logger.log_info("✅ 飞牛音乐配置保存并测试登录成功！")
    else:
        music_logger.log_error(f"❌ 飞牛音乐配置已保存但测试登录失败: {fail_msg}")

    return {
        "ok": True,
        "login_success": login_ok,
        "message": "飞牛音乐账户配置已更新并验证成功！" if login_ok else fail_msg
    }


@app.get("/api/music/songs")
async def get_music_songs(request: Request):
    """Discover and return playable music tracks from music_db and fnOS."""
    host = music_db.get_music_config("fnmusic_host", db.get_config("nas_ip", "127.0.0.1"))
    port = music_db.get_music_config("fnmusic_port", 5888)
    songs = music_db.get_playlist()

    req_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    req_domain = req_host.split(":")[0] if req_host else ""
    target_host = host
    if req_domain and req_domain not in ("127.0.0.1", "localhost", "0.0.0.0"):
        target_host = req_domain

    # If local playlist is empty, auto-sync from fnOS Music
    if not songs:
        try:
            fn_tracks = await fnmusic_client.get_tracks(page=1, page_size=50)
            if fn_tracks:
                music_db.sync_fnos_tracks(fn_tracks, replace=False)
                songs = music_db.get_playlist()
        except Exception as e:
            logger.error("Auto sync fnos tracks error: %s", e)

    for s in songs:
        u = s.get("url", "")
        s["src"] = u

    return {
        "ok": True,
        "songs": songs,
        "fnos_music_url": f"http://{target_host}:{port}/music/"
    }


@app.get("/api/music/playlists")
async def get_music_playlists():
    """Retrieve existing playlists directly from fnOS Music."""
    try:
        raw_playlists = await fnmusic_client.get_playlists()
        result = []
        for p in raw_playlists:
            guid = p.get("guid") or ""
            name = p.get("name") or p.get("title") or "未命名歌单"
            cover_url = p.get("coverUrl") or ""
            cover_id = p.get("coverId") or ""
            
            cover = ""
            if cover_url and cover_url.startswith("http"):
                cover = cover_url
            elif cover_id:
                cover = f"/api/music/fnos/cover?coverId={cover_id}"
            
            track_count = p.get("trackCount") or p.get("track_count") or 0
            result.append({
                "id": guid,
                "name": name,
                "cover": cover,
                "track_count": track_count,
                "is_daily": bool(p.get("isDaily")),
                "updated_at": p.get("updatedAt", 0)
            })
        
        # 优先把用户自建歌单排在最前，并在首位增加“全部本地音乐”
        all_local_card = {
            "id": "local:all",
            "name": "🎵 全部本地音乐 (本地曲库)",
            "cover": "",
            "track_count": "本地",
            "is_daily": True,
            "updated_at": 0
        }
        custom_pls = [p for p in result if not p["id"].startswith("online:")]
        online_pls = [p for p in result if p["id"].startswith("online:")]
        final_list = [all_local_card] + custom_pls + online_pls

        return {"ok": True, "playlists": final_list}
    except Exception as e:
        logger.error("Error fetching playlists: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/music/playlists/{playlist_id}/tracks")
async def get_playlist_tracks(playlist_id: str):
    """Retrieve tracks for a specific playlist from fnOS Music."""
    try:
        if playlist_id == "local:all":
            raw_tracks = await fnmusic_client.get_all_tracks(max_tracks=3000)
        else:
            raw_tracks = await fnmusic_client.get_all_playlist_tracks(playlist_id, max_tracks=3000)
        formatted = []
        for t in raw_tracks:
            guid = t.get("guid") or t.get("id") or ""
            if not guid:
                continue
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

            formatted.append({
                "id": f"fn_{guid}",
                "guid": guid,
                "title": title,
                "artist": artist,
                "album": album or "飞牛音乐",
                "cover": f"/api/music/fnos/cover?coverId={cover_id}" if cover_id else "",
                "url": f"/api/music/fnos/stream?guid={guid}",
                "duration": duration
            })
        return {"ok": True, "tracks": formatted}
    except Exception as e:
        logger.error("Error fetching playlist tracks: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/music/playlists/{playlist_id}/play")
async def play_playlist(playlist_id: str):
    """Load all tracks of a playlist into the active playlist in music.db and return them."""
    try:
        if playlist_id == "local:all":
            raw_tracks = await fnmusic_client.get_all_tracks(max_tracks=3000)
        else:
            raw_tracks = await fnmusic_client.get_all_playlist_tracks(playlist_id, max_tracks=3000)
        if not raw_tracks:
            return JSONResponse({"ok": False, "error": "歌单内没有找到歌曲或获取失败"}, status_code=404)
        
        music_db.sync_fnos_tracks(raw_tracks, replace=True)
        songs = music_db.get_playlist()
        return {"ok": True, "count": len(songs), "songs": songs}
    except Exception as e:
        logger.error("Error playing playlist: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/music/sync")
async def sync_music_from_fnos():
    """Manually trigger fetching latest tracks from fnOS Music service."""
    try:
        fn_tracks = await fnmusic_client.get_all_tracks(max_tracks=3000)
        if fn_tracks:
            music_db.sync_fnos_tracks(fn_tracks, replace=True)
            songs = music_db.get_playlist()
            return {"ok": True, "count": len(songs), "songs": songs}
        else:
            return JSONResponse({"ok": False, "error": "未能从飞牛音乐拉取到歌曲，请检查 5888 端口与账号"}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/music/search")
async def search_music_from_fnos(q: str = ""):
    """Search tracks directly in fnOS Music library."""
    if not q:
        return {"ok": True, "tracks": []}
    try:
        results = await fnmusic_client.search_tracks(q, page=1, page_size=30)
        formatted = []
        for t in results:
            guid = t.get("guid", "")
            artists = ", ".join([a.get("name", "") for a in t.get("artists", []) if a.get("name")])
            album = t.get("album", {}).get("name", "") if isinstance(t.get("album"), dict) else ""
            cover_id = t.get("coverId", "")
            formatted.append({
                "guid": guid,
                "title": t.get("title", ""),
                "artist": artists or "未知艺术家",
                "album": album or "飞牛音乐",
                "cover": f"/api/music/fnos/cover?coverId={cover_id}" if cover_id else "",
                "stream_url": f"/api/music/fnos/stream?guid={guid}",
                "duration": t.get("duration", 0),
            })
        return {"ok": True, "tracks": formatted}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/music/songs")
async def add_music_song(request: Request, _auth: dict = Depends(require_auth)):
    """Add a new song to music.db (independent from nas-stats.db)."""
    body = await request.json()
    title = body.get("title", "").strip()
    url = body.get("url", "").strip()
    if not title or not url:
        return JSONResponse({"error": "歌曲标题和音频链接不能为空"}, status_code=400)
    artist = body.get("artist", "").strip()
    album = body.get("album", "").strip()
    cover = body.get("cover", "").strip()
    guid = body.get("guid", "").strip()
    source = "fnos" if guid else "custom"
    song_id = music_db.add_song(title, artist, album, url, cover, source=source, guid=guid)
    return {"ok": True, "id": song_id}


@app.delete("/api/music/songs/{song_id}")
async def delete_music_song(song_id: str, _auth: dict = Depends(require_auth)):
    """Delete a song from music.db."""
    music_db.delete_song(song_id)
    return {"ok": True}


@app.post("/api/music/reset")
async def reset_music_songs(_auth: dict = Depends(require_auth)):
    """Reset music playlist to default tracks."""
    music_db.reset_playlist()
    return {"ok": True}


@app.api_route("/api/music/fnos/stream", methods=["GET", "HEAD", "OPTIONS"])
async def fnos_music_stream(guid: str, request: Request):
    """Stream audio directly from fnOS Music backend with Range, HEAD, and CORS support."""
    cors_headers = {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "GET, HEAD, OPTIONS",
        "access-control-allow-headers": "Range, Content-Type, Accept, Authorization",
        "access-control-expose-headers": "Content-Length, Content-Range, Accept-Ranges",
        "x-accel-buffering": "no",
        "cache-control": "no-cache, no-transform",
    }

    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors_headers)

    if not guid:
        return JSONResponse({"error": "missing guid"}, status_code=400, headers=cors_headers)

    await fnmusic_client.ensure_token()

    target_path = "/music/api/v1/track/stream"
    params = {"guid": guid}
    url = f"{fnmusic_client.base_url}{target_path}"

    rng = request.headers.get("range", "none")
    music_logger.log_info(f"🎧 收到音频流请求 ({request.method}): guid={guid[:12]}..., Range={rng}")

    stream_timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=None)

    # Perform request with auto-retry if upstream returns 100001 unknown error / session invalid
    for attempt in range(2):
        token = fnmusic_client.token
        authx = fnmusic_client._sign_authx("GET", target_path, params=params)
        req_headers = {
            "authx": authx,
            "Cookie": f"music-token={token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        if "range" in request.headers:
            req_headers["range"] = request.headers["range"]

        try:
            client = httpx.AsyncClient(timeout=stream_timeout, trust_env=False, follow_redirects=True)
            upstream_req = client.build_request("GET", url, params=params, headers=req_headers)
            resp = await client.send(upstream_req, stream=True)

            ct = resp.headers.get("content-type", "")

            # If upstream returned JSON or error status (e.g. 100001 unknown error, 100004 forbidden, 401 unauthorized)
            if "application/json" in ct or resp.status_code >= 400:
                content = await resp.aread()
                await resp.aclose()
                await client.aclose()

                # Check if this error is recoverable by re-authenticating (e.g. code 100001, 100004, 401, 403)
                is_recoverable = False
                err_code = None
                try:
                    err_json = json.loads(content)
                    err_code = err_json.get("code")
                    if err_code in (100001, 100002, 100004) or resp.status_code in (401, 403):
                        is_recoverable = True
                except Exception:
                    if resp.status_code in (401, 403):
                        is_recoverable = True

                if is_recoverable and attempt == 0:
                    music_logger.log_warn(
                        f"⚠️ 音频流触发上游错误 (code={err_code or resp.status_code})，检测到会话失效，正在自动重新鉴权恢复..."
                    )
                    login_ok = await fnmusic_client.force_relogin()
                    if login_ok:
                        music_logger.log_info(f"🔄 飞牛音乐鉴权恢复成功，正在重试拉取音频流: guid={guid[:12]}...")
                        continue

                music_logger.log_warn(
                    f"⚠️ 音频流请求异常: guid={guid[:12]}..., HTTP {resp.status_code}, 内容: {content.decode('utf-8', errors='ignore')[:100]}"
                )
                return Response(
                    content=content,
                    status_code=403 if resp.status_code == 200 else resp.status_code,
                    media_type="application/json",
                    headers=cors_headers
                )

            resp_headers = dict(cors_headers)
            resp_headers["accept-ranges"] = "bytes"
            for h in ("content-length", "content-range", "content-disposition"):
                if h in resp.headers:
                    resp_headers[h] = resp.headers[h]

            media_type = ct if (ct and "octet-stream" not in ct) else "audio/mpeg"

            # Handle HEAD request probe from iOS Safari / mobile browsers
            if request.method == "HEAD":
                await resp.aclose()
                await client.aclose()
                return Response(
                    content=b"",
                    status_code=resp.status_code,
                    headers=resp_headers,
                    media_type=media_type
                )

            cl = resp.headers.get("content-length", "未知")
            music_logger.log_info(f"▶️ 开始传输音频流: guid={guid[:12]}..., HTTP {resp.status_code}, 格式: {media_type}, 大小: {cl}")

            async def stream_generator():
                try:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    pass
                except Exception as e:
                    logger.debug("fnos stream proxy client disconnected: %s", e)
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                stream_generator(),
                status_code=resp.status_code,
                headers=resp_headers,
                media_type=media_type
            )
        except Exception as e:
            if attempt == 0:
                music_logger.log_warn(f"⚠️ 音频流连接波动: {e}，尝试重新连通...")
                await fnmusic_client.force_relogin()
                continue
            music_logger.log_error(f"❌ 音频流传输异常: guid={guid[:12]}..., 错误: {e}")
            logger.error("fnos stream proxy error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=502, headers=cors_headers)


@app.api_route("/api/music/fnos/cover", methods=["GET", "HEAD", "OPTIONS"])
async def fnos_music_cover(coverId: str, request: Request):
    """Proxy cover images from fnOS Music with auto-relogin retry and CORS."""
    cors_headers = {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "GET, HEAD, OPTIONS",
        "cache-control": "public, max-age=86400",
    }
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors_headers)
    if not coverId:
        return JSONResponse({"error": "missing coverId"}, status_code=400, headers=cors_headers)

    await fnmusic_client.ensure_token()

    target_path = "/music/api/v1/static/cover"
    params = {"coverId": coverId}
    url = f"{fnmusic_client.base_url}{target_path}"

    for attempt in range(2):
        token = fnmusic_client.token
        authx = fnmusic_client._sign_authx("GET", target_path, params=params)
        req_headers = {
            "authx": authx,
            "Cookie": f"music-token={token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as cov_client:
                resp = await cov_client.get(url, params=params, headers=req_headers, follow_redirects=True)
                ct = resp.headers.get("content-type", "")
                if resp.status_code == 200 and not ct.startswith("application/json"):
                    content = b"" if request.method == "HEAD" else resp.content
                    return Response(content=content, media_type=ct or "image/webp", headers=cors_headers)
                if (resp.status_code in (401, 403) or "application/json" in ct) and attempt == 0:
                    if await fnmusic_client.force_relogin():
                        continue
                return JSONResponse({"error": "cover not found"}, status_code=resp.status_code if resp.status_code >= 400 else 404, headers=cors_headers)
        except Exception as e:
            if attempt == 0:
                continue
            return JSONResponse({"error": str(e)}, status_code=502, headers=cors_headers)


@app.api_route("/api/music/fnos/lyric", methods=["GET", "OPTIONS"])
async def fnos_music_lyric(guid: str = "", request: Request = None):
    """Retrieve lyric content for a track from fnOS Music."""
    cors_headers = {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "GET, OPTIONS",
    }
    if request and request.method == "OPTIONS":
        return Response(status_code=204, headers=cors_headers)
    if not guid:
        return JSONResponse({"ok": False, "error": "missing guid"}, status_code=400, headers=cors_headers)
    try:
        data = await fnmusic_client.get_lyrics(guid)
        if data:
            return JSONResponse({"ok": True, "guid": guid, "data": data}, headers=cors_headers)
        return JSONResponse({"ok": False, "error": "lyric not found"}, status_code=404, headers=cors_headers)
    except Exception as e:
        logger.error("fnos lyric proxy error: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502, headers=cors_headers)


@app.post("/api/music/fnos/reconnect")
async def fnos_music_reconnect(_auth: dict = Depends(require_auth)):
    """Force re-authenticate with fnOS Music service and perform diagnostic health check."""
    music_logger.log_info("🔄 收到飞牛音乐手动重新连接与诊断请求...")
    fnmusic_client.invalidate_token()
    load_fnmusic_config()
    login_ok = await fnmusic_client.login()
    if not login_ok:
        err = fnmusic_client.last_error or "连接失败，无法通过 5888 端口验证飞牛账号"
        music_logger.log_error(f"❌ 飞牛音乐手动重新连接失败: {err}")
        return JSONResponse({"ok": False, "message": err}, status_code=500)

    # Perform lightweight probe to verify 5888 API is responsive
    tracks = await fnmusic_client.get_tracks(page=1, page_size=5)
    music_logger.log_info(f"✅ 飞牛音乐诊断正常，成功连通服务并读取到 {len(tracks)} 首歌曲")
    return {
        "ok": True,
        "message": f"飞牛音乐服务连接正常！已成功刷新 Token 并连通曲库（发现 {len(tracks)} 首歌曲）。",
        "sample_count": len(tracks)
    }


# ── Music Logs API ──────────────────────────────────────────────────

@app.get("/api/music/logs")
async def get_music_logs(limit: int = 150, level: str = "ALL", keyword: str = "", _auth: dict = Depends(require_auth)):
    """Retrieve filtered logs from data/music.log buffer (requires auth)."""
    logs = music_logger.get_logs(limit=limit, level=level, keyword=keyword)
    return {
        "ok": True,
        "total": len(logs),
        "logs": logs
    }


@app.post("/api/music/logs/clear")
async def clear_music_logs(_auth: dict = Depends(require_auth)):
    """Clear music logs (requires auth)."""
    ok = music_logger.clear_logs()
    return {"ok": ok, "message": "音乐运行日志已清空" if ok else "清空失败"}


@app.get("/api/music/logs/download")
async def download_music_logs(_auth: dict = Depends(require_auth)):
    """Download data/music.log directly (requires auth)."""
    if not music_logger.MUSIC_LOG_FILE.exists():
        with open(music_logger.MUSIC_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
    return FileResponse(
        path=music_logger.MUSIC_LOG_FILE,
        filename="music.log",
        media_type="text/plain; charset=utf-8"
    )


@app.get("/api/music/stream-proxy")
async def music_stream_proxy(request: Request, url: str = "", _auth: dict = Depends(require_auth)):
    url = validate_proxy_url(url, allow_private=True)
    try:
        req_headers = {}
        if "range" in request.headers:
            req_headers["range"] = request.headers["range"]
        resp = await client.get(url, headers=req_headers, follow_redirects=True, timeout=15)
        resp_headers = {}
        for h in ("content-type", "content-length", "content-range", "accept-ranges"):
            if h in resp.headers:
                resp_headers[h] = resp.headers[h]
        return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers, media_type=resp.headers.get("content-type", "audio/mpeg"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)




# ── Wallpaper / Bing ────────────────────────────────────────────────

bing_cache = {"data": None, "ts": 0}
BING_CACHE_TTL = 4 * 3600


async def _fetch_bing_images():
    http_cli = get_http_client(timeout=8.0)
    for host in ["https://cn.bing.com", "https://www.bing.com"]:
        try:
            resp = await http_cli.get(
                f"{host}/HPImageArchive.aspx",
                params={"format": "js", "idx": "0", "n": "8", "mkt": "zh-CN"},
                follow_redirects=True, timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                images = []
                for img in data.get("images", []):
                    url_path = img.get("url", "")
                    if not url_path:
                        continue
                    full_url = host + url_path
                    images.append({
                        "url": "/api/bg-proxy?url=" + full_url,
                        "title": img.get("copyright", ""),
                        "date": img.get("startdate", ""),
                    })
                if images:
                    return images
        except Exception:
            continue
    raise ValueError("All Bing endpoints failed")


async def _fetch_picsum_images():
    images = []
    for i in range(8):
        images.append({
            "url": f"https://picsum.photos/1920/1080?random={i}&t={int(time.time())}",
            "title": "随机壁纸",
            "date": "",
        })
    return images


@app.get("/api/bing-bg")
async def bing_bg():
    now = time.time()
    if bing_cache["data"] and now - bing_cache["ts"] < BING_CACHE_TTL:
        return bing_cache["data"]

    for source_name, fetcher in [("bing", _fetch_bing_images), ("picsum", _fetch_picsum_images)]:
        try:
            images = await fetcher()
            result = {"ok": True, "images": images, "source": source_name}
            bing_cache["data"] = result
            bing_cache["ts"] = now
            return result
        except Exception:
            pass

    return {"ok": False, "error": "all sources failed", "images": []}


@app.get("/api/bg-proxy")
async def bg_proxy(url: str = ""):
    url = validate_proxy_url(url, allow_private=False)
    http_cli = get_http_client(timeout=12.0)
    try:
        resp = await http_cli.get(url, follow_redirects=True, timeout=12)
        ct = resp.headers.get("content-type", "image/jpeg")
        if not ct.startswith("image/"):
            return JSONResponse({"error": "Only image content allowed"}, status_code=400)
        # Limit background proxy size to 15MB
        if len(resp.content) > 15 * 1024 * 1024:
            return JSONResponse({"error": "Image too large"}, status_code=413)
        return Response(
            content=resp.content,
            media_type=ct,
            headers={"Cache-Control": "public, max-age=86400"}
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ── Services Management (CRUD) ──────────────────────────────────────

@app.api_route("/api/services", methods=["GET", "POST"])
async def handle_services(request: Request, action: str = Query(...)):
    if action == "list":
        services = db.get_all_services()
        return {"items": services}

    # All mutating actions require active admin auth
    await require_auth(request)

    body = await request.json() if request.method == "POST" else {}

    if action == "toggle":
        db.update_service(body.get("id"), visible=int(body.get("visible", True)))
        return {"ok": True}

    if action == "update":
        sid = body.get("id")
        updates = {
            "name": body.get("name"),
            "icon": body.get("icon", ""),
            "description": body.get("desc", body.get("description", "")),
            "url": body.get("url", ""),
            "wan_url": body.get("wan_url", ""),
            "port": body.get("port"),
            "protocol": body.get("protocol", "http"),
            "favicon": body.get("favicon", ""),
        }
        updates = {k: v for k, v in updates.items() if v is not None}
        db.update_service(sid, **updates)
        return {"ok": True}

    if action == "delete":
        db.delete_service(body.get("id"))
        return {"ok": True}

    if action == "batch_delete":
        ids = body.get("ids", [])
        db.delete_services_batch(ids)
        return {"ok": True, "count": len(ids)}

    if action == "batch_fetch_icons":
        services = db.get_all_services()
        updated_count = 0
        target_ids = body.get("ids")  # if provided, only for those ids, else for all with URLs

        async def _fetch_one(s):
            nonlocal updated_count
            sid = s["id"]
            url = s.get("url", "").strip()
            if not url or not url.startswith(("http://", "https://")):
                return
            if target_ids and sid not in target_ids:
                return
            try:
                # Reuse meta probing logic
                parsed = urlparse(url)
                resp = await client.get(url, follow_redirects=True, timeout=5)
                html = resp.text[:40000]
                favicon_url = ""
                for pat in [
                    r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]+href=["\']([^"\']+)["\']',
                    r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'](?:shortcut )?icon["\']',
                    r'<link[^>]+rel=["\']apple-touch-icon["\'][^>]+href=["\']([^"\']+)["\']',
                ]:
                    m = re.search(pat, html, re.IGNORECASE)
                    if m:
                        favicon_url = m.group(1)
                        break
                if not favicon_url:
                    favicon_url = "/favicon.ico"

                icon_data = ""
                if favicon_url.startswith("data:"):
                    icon_data = favicon_url
                else:
                    abs_icon = urljoin(url, favicon_url)
                    ir = await client.get(abs_icon, follow_redirects=True, timeout=3)
                    if ir.status_code == 200:
                        ct = ir.headers.get("content-type", "image/x-icon")
                        b64 = base64.b64encode(ir.content).decode()
                        icon_data = f"data:{ct};base64,{b64}"

                if icon_data:
                    db.update_service(sid, favicon=icon_data)
                    updated_count += 1
            except Exception:
                pass

        tasks = [_fetch_one(s) for s in services]
        await asyncio.gather(*tasks, return_exceptions=True)
        return {"ok": True, "updated": updated_count}

    if action == "add":
        count = db.count_services()
        new_item = {
            "id": str(uuid.uuid4()),
            "port": body.get("port", 0),
            "name": body.get("name", ""),
            "category": body.get("category", "自定义"),
            "protocol": body.get("protocol", "http"),
            "icon": body.get("icon", "🔧"),
            "description": body.get("desc", body.get("description", "")),
            "url": body.get("url", ""),
            "wan_url": body.get("wan_url", ""),
            "favicon": body.get("favicon", ""),
            "visible": 1,
            "sort_order": count,
        }
        db.add_service(new_item)
        return {"ok": True, "id": new_item["id"]}

    if action == "reorder":
        order_map = {o["id"]: o["sort_order"] for o in body.get("order", [])}
        db.reorder_services(order_map)
        return {"ok": True}

    if action == "import":
        existing_ports = db.get_service_ports()
        count = db.count_services()
        added, skipped = 0, 0
        for item in body.get("services", []):
            if item.get("port") in existing_ports:
                skipped += 1
                continue
            db.add_service({
                "id": str(uuid.uuid4()),
                "port": item["port"],
                "name": item.get("name", ""),
                "category": item.get("category", "自定义"),
                "protocol": item.get("protocol", "http"),
                "icon": item.get("icon", "🔧"),
                "description": item.get("desc", item.get("description", "")),
                "url": item.get("url", ""),
                "wan_url": item.get("wan_url", ""),
                "favicon": item.get("favicon", ""),
                "visible": 1,
                "sort_order": count + added,
            })
            existing_ports.add(item["port"])
            added += 1
        return {"ok": True, "added": added, "skipped": skipped}

    if action == "seed":
        from main_defaults import DEFAULT_SERVICES
        existing_ports = db.get_service_ports()
        count = db.count_services()
        added = 0
        for s in DEFAULT_SERVICES:
            if s["port"] in existing_ports:
                continue
            db.add_service({
                "id": str(uuid.uuid4()),
                "port": s["port"],
                "name": s["name"],
                "category": s["category"],
                "protocol": s["protocol"],
                "icon": s["icon"],
                "description": s["description"],
                "url": s["url"],
                "favicon": "",
                "visible": 1,
                "sort_order": count + added,
            })
            existing_ports.add(s["port"])
            added += 1
        return {"ok": True, "count": added}

    return JSONResponse({"error": "unknown action"}, status_code=400)


# ── Discovery & Scans ───────────────────────────────────────────────

KNOWN_SERVICES = {
    # 基础协议与远程管理
    21: ("FTP 文件服务", "📁", "tcp"),
    22: ("SSH 终端服务", "🔒", "tcp"),
    23: ("Telnet", "🖥️", "tcp"),
    25: ("SMTP 邮件服务", "📧", "tcp"),
    53: ("DNS 域名解析", "🌐", "tcp"),
    80: ("HTTP 网站服务", "🌐", "http"),
    81: ("Nginx 代理", "🌐", "http"),
    88: ("Heimdall 导航", "🏠", "http"),
    110: ("POP3 邮件接收", "📧", "tcp"),
    135: ("RPC 远程调用", "🖥️", "tcp"),
    139: ("NetBIOS 文件共享", "📂", "tcp"),
    143: ("IMAP 邮件服务", "📧", "tcp"),
    443: ("HTTPS 安全网站", "🔒", "https"),
    444: ("Heimdall (HTTPS)", "🏠", "https"),
    445: ("SMB 文件共享", "📂", "tcp"),
    548: ("AFP 苹果文件共享", "📂", "tcp"),
    631: ("CUPS 打印服务", "🖨️", "http"),
    873: ("rsync 数据同步", "🔄", "tcp"),
    993: ("IMAPS 加密邮件", "📧", "tcp"),
    995: ("POP3S 加密邮件", "📧", "tcp"),
    1080: ("Socks 代理服务", "🛡️", "tcp"),
    1433: ("MSSQL 数据库", "🗄️", "tcp"),
    1521: ("Oracle 数据库", "🗄️", "tcp"),
    1880: ("Node-RED 自动化", "🔧", "http"),
    1883: ("MQTT 物联网消息", "📡", "tcp"),
    2049: ("NFS 网络文件共享", "📂", "tcp"),
    2375: ("Docker 引擎 API", "🐳", "http"),
    2376: ("Docker TLS API", "🐳", "tcp"),
    2829: ("Nginx 欢迎页", "🌐", "http"),
    3000: ("MoviePilot / 前端", "🎥", "http"),
    3001: ("Uptime Kuma / API", "📊", "http"),
    3003: ("天翼云盘转存", "☁️", "http"),
    3004: ("Vertex PT 管理", "🔄", "http"),
    3006: ("PanSou 盘搜", "🔍", "http"),
    3009: ("在线电视直播", "📡", "http"),
    3011: ("Uptime Kuma 监控", "📊", "http"),
    3012: ("AI 智能错题本", "❌", "http"),
    3018: ("字幕助手", "💬", "http"),
    3019: ("Ting Reader 有声书", "🔊", "http"),
    3020: ("在线音乐播放器", "🐟", "http"),
    3024: ("IPTV 控制中心", "📺", "http"),
    3027: ("字幕助手 (备)", "💬", "http"),
    3028: ("Vigil 镜像监控", "👁️", "http"),
    3029: ("MoonTV 影视", "🌙", "http"),
    3031: ("WorkBuddy 用量看板", "📈", "http"),
    3128: ("Squid 代理", "🌐", "tcp"),
    3242: ("Web 应用", "🌐", "http"),
    3306: ("MySQL 数据库", "🗄️", "tcp"),
    3307: ("MySQL 备用", "🗄️", "tcp"),
    3389: ("Windows 远程桌面 RDP", "🖥️", "tcp"),
    4000: ("Halo / Hexo 博客", "📝", "http"),
    4040: ("Subsonic 音频流", "🎵", "http"),
    4443: ("HTTPS 应用", "🔒", "https"),
    4533: ("Navidrome 音乐服务器", "🎵", "http"),
    45849: ("Go2RTC 视频流转发", "📹", "http"),
    5000: ("群晖 DSM / Web 应用", "🌐", "http"),
    5001: ("群晖 DSM (HTTPS)", "🔒", "https"),
    5005: ("WebDAV / Python App", "🐍", "http"),
    5006: ("WebDAV (HTTPS)", "🔒", "https"),
    5055: ("Overseerr 影视订阅", "🎬", "http"),
    5080: ("Web 视频/流媒体", "🌐", "http"),
    5212: ("Cloudreve 自建云盘", "☁️", "http"),
    5230: ("Memos 自托管备忘录", "🗒️", "http"),
    5244: ("Alist 网盘聚合列表", "📁", "http"),
    5245: ("Alist 备用端口", "📁", "http"),
    5432: ("PostgreSQL 数据库", "🗄️", "tcp"),
    5601: ("Kibana 可视化平台", "📊", "http"),
    5678: ("n8n 自动化工作流", "⚡", "http"),
    5700: ("青龙自动化面板", "🐉", "http"),
    5800: ("JDownloader / VNC Web", "📥", "http"),
    5888: ("飞牛 fnOS 系统面板", "🐂", "http"),
    5889: ("飞牛 fnOS (HTTPS)", "🐂", "https"),
    5900: ("VNC 远程桌面", "🖥️", "tcp"),
    5901: ("VNC 远程桌面", "🖥️", "tcp"),
    6379: ("Redis 内存数据库", "⚡", "tcp"),
    6598: ("Web 应用", "🌐", "http"),
    6690: ("Synology Drive 同步", "📂", "tcp"),
    6767: ("BaiduPCS-Go 百度网盘", "📁", "http"),
    6800: ("Aria2 下载 RPC", "📥", "http"),
    6881: ("BitTorrent 传入", "📥", "tcp"),
    7000: ("Lucky / frp 反向代理", "🍀", "https"),
    7500: ("frp 管理面板", "📡", "http"),
    7860: ("Stable Diffusion / Gradio", "🎨", "http"),
    7878: ("Radarr 电影管理", "🎬", "http"),
    7890: ("Clash 代理服务", "🛡️", "http"),
    8000: ("Web 应用服务", "🌐", "http"),
    8005: ("飞牛影视原生应用", "🎞️", "http"),
    8006: ("Proxmox VE 虚拟化", "🖥️", "https"),
    8008: ("Web 媒体应用", "🌐", "http"),
    8010: ("Web 应用", "🌐", "http"),
    8020: ("Web 应用", "🌐", "http"),
    8024: ("Python Web 应用", "🐍", "http"),
    8080: ("通用 Web 代理 / 容器", "🌐", "http"),
    8081: ("Web 应用", "🌐", "http"),
    8082: ("轻阅读 / Web 应用", "📖", "http"),
    8083: ("轻课堂 / HTTPS 应用", "🎓", "https"),
    8085: ("qBittorrent 下载器", "⬇️", "http"),
    8086: ("InfluxDB 时序数据库", "📈", "http"),
    8087: ("Web 应用", "🌐", "http"),
    8088: ("Web 应用", "🌐", "http"),
    8089: ("Web 应用", "🌐", "http"),
    8090: ("talebook 电子图书馆", "📚", "http"),
    8091: ("Web 应用", "🌐", "http"),
    8095: ("Web 应用", "🌐", "http"),
    8096: ("Jellyfin / Emby 媒体库", "📺", "http"),
    8099: ("SQMusic 音乐播放器", "🎵", "http"),
    8112: ("Deluge BT 下载器", "📥", "http"),
    8123: ("Home Assistant 智能家居", "🏠", "http"),
    8181: ("Web 应用", "🌐", "http"),
    8191: ("Flaresolverr 验证码绕过", "🛡️", "http"),
    8200: ("Vault 密钥管理", "🔒", "http"),
    8222: ("Web 应用", "🌐", "http"),
    8282: ("1Panel 运维面板备用", "🛠️", "http"),
    8334: ("Web 应用", "🌐", "http"),
    8384: ("Syncthing 文件同步", "🔄", "http"),
    8443: ("Lucky / HTTPS 反向代理", "🍀", "https"),
    8448: ("Matrix 通信服务", "💬", "https"),
    8501: ("Streamlit 仪表盘", "📊", "http"),
    8554: ("RTSP 监控视频流", "📹", "tcp"),
    8686: ("Lidarr 音乐管理", "🎵", "http"),
    8689: ("Leelaa Playlist 播放列表", "📋", "http"),
    8787: ("GalaxyJAV / Readarr", "🌌", "http"),
    8848: ("Nacos 配置中心", "⚙️", "http"),
    8880: ("Web 代理端口", "🌐", "http"),
    8888: ("1Panel / 宝塔 / Jupyter", "🛠️", "http"),
    8901: ("个人网址收藏夹", "🔖", "http"),
    8920: ("Jellyfin / Emby (HTTPS)", "📺", "https"),
    8923: ("QD 自动签到框架", "✅", "http"),
    8980: ("NAS Stats 仪表盘", "🖥️", "http"),
    8989: ("Sonarr 剧集自动化", "📺", "http"),
    9000: ("Portainer 容器 / MinIO", "🐳", "http"),
    9001: ("MinIO 控制台", "🗄️", "http"),
    9002: ("Web 应用", "🌐", "http"),
    9090: ("hlink / Prometheus", "🔗", "http"),
    9091: ("Transmission 下载管理", "📥", "http"),
    9100: ("Node Exporter 监控探针", "📊", "http"),
    9117: ("Prowlarr / Jackett 索引器", "🔍", "http"),
    9200: ("Elasticsearch 搜索引擎", "🔍", "http"),
    9280: ("WizNote 为知笔记", "📝", "http"),
    9443: ("Portainer (HTTPS)", "🔒", "https"),
    9696: ("Prowlarr 聚合索引器", "🔍", "http"),
    9980: ("TVHeadend 电视直播", "📺", "http"),
    9990: ("飞牛桌面管理工具", "🖥️", "http"),
    9999: ("Web 应用 / Remux", "🌐", "http"),
    10000: ("EasyNVR 监控 / Webmin", "📹", "http"),
    10050: ("Zabbix 监控客户端", "📊", "tcp"),
    10086: ("1Panel 现代化服务器面板", "🛠️", "http"),
    10101: ("Web 应用", "🌐", "http"),
    10789: ("BestNav 飞牛导航页", "🧭", "http"),
    10808: ("代理客户端 SOCKS/HTTP", "🛡️", "tcp"),
    10809: ("代理客户端 HTTP", "🛡️", "tcp"),
    11434: ("Ollama 本地 AI 大模型", "🦙", "http"),
    13378: ("Audiobookshelf 有声书", "🎧", "http"),
    14000: ("Web 应用", "🌐", "http"),
    18080: ("Web 代理端口", "🌐", "http"),
    18090: ("phpMyAdmin 数据库管理", "🗄️", "http"),
    18230: ("FnMessageBot 飞牛机器人", "🤖", "http"),
    18787: ("IYUUPlus 辅种利器", "📨", "http"),
    19999: ("Netdata 系统实时监控", "📊", "http"),
    27017: ("MongoDB 数据库", "🍃", "tcp"),
    28017: ("MongoDB Web 监控", "🍃", "http"),
    32400: ("Plex 媒体中心", "▶️", "http"),
    45849: ("Go2RTC 视频转发", "📹", "http"),
    49152: ("UPnP 局域网服务", "🔗", "tcp"),
    49153: ("UPnP 服务", "🔗", "tcp"),
    49154: ("UPnP 服务", "🔗", "tcp"),
    51413: ("Transmission BT 传入", "⬇️", "tcp"),
    51414: ("BT 传入端口", "⬇️", "tcp"),
    51415: ("BT 传入端口", "⬇️", "tcp"),
    61208: ("Glances 系统监控", "📊", "http"),
}

CORE_SCAN_PORTS = sorted(list(set(
    list(KNOWN_SERVICES.keys()) +
    [
        80, 81, 82, 83, 84, 85, 86, 87, 88, 89,
        443, 444, 445, 446, 447, 448,
        *list(range(3000, 3051)),
        *list(range(5000, 5021)),
        *list(range(5880, 5896)),
        *list(range(8000, 8026)),
        *list(range(8080, 8111)),
        *list(range(8880, 8896)),
        *list(range(8980, 8996)),
        *list(range(9000, 9021)),
        *list(range(9090, 9106)),
        *list(range(10000, 10021)),
        *list(range(18080, 18101)),
    ]
)))


def parse_port_specs(spec: str) -> list[int]:
    """Parse ports spec like '80,443,5000-5010,8080-8090' into sorted unique list."""
    ports = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                s_int, e_int = int(start.strip()), int(end.strip())
                if s_int > e_int:
                    s_int, e_int = e_int, s_int
                s_int = max(1, s_int)
                e_int = min(65535, e_int)
                for p in range(s_int, e_int + 1):
                    ports.add(p)
                    if len(ports) >= 10000:
                        break
            except Exception:
                pass
        else:
            try:
                p_int = int(part)
                if 1 <= p_int <= 65535:
                    ports.add(p_int)
            except Exception:
                pass
        if len(ports) >= 10000:
            break
    return sorted(ports)


async def _probe_port(ip: str, port: int, sem: asyncio.Semaphore) -> int | None:
    async with sem:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=0.6)
            w.close()
            await w.wait_closed()
            return port
        except Exception:
            return None


async def _probe_web_service(ip: str, port: int, client: httpx.AsyncClient | None) -> dict:
    http_client = client if (client and not client.is_closed) else httpx.AsyncClient(timeout=4, verify=False, follow_redirects=True)
    is_likely_ssl = port in (443, 444, 5001, 5889, 7000, 8006, 8083, 8443, 8920, 9443)
    proto_order = ["https", "http"] if is_likely_ssl else ["http", "https"]

    for proto in proto_order:
        url = f"{proto}://{ip}:{port}"
        try:
            resp = await http_client.get(url, follow_redirects=True, timeout=3.5)
            if proto == "http" and resp.status_code == 400 and ("HTTPS" in resp.text or "SSL" in resp.text):
                continue

            html = resp.text[:40000]
            title = ""
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if m:
                title = html_lib.unescape(m.group(1).strip())

            favicon_url = ""
            for pat in [
                r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]+href=["\']([^"\']+)["\']',
                r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'](?:shortcut )?icon["\']',
                r'<link[^>]+rel=["\']apple-touch-icon["\'][^>]+href=["\']([^"\']+)["\']',
                r'<link[^>]+rel=["\']icon["\'][^>]+href=["\']([^"\']+)["\']',
            ]:
                fm = re.search(pat, html, re.IGNORECASE)
                if fm:
                    favicon_url = fm.group(1)
                    break
            if not favicon_url:
                favicon_url = "/favicon.ico"

            favicon = ""
            if favicon_url.startswith("data:"):
                favicon = favicon_url
            else:
                abs_icon = urljoin(url, favicon_url)
                try:
                    ir = await http_client.get(abs_icon, follow_redirects=True, timeout=2.5)
                    if ir.status_code == 200:
                        ct = ir.headers.get("content-type", "image/x-icon")
                        b64 = base64.b64encode(ir.content).decode()
                        favicon = f"data:{ct};base64,{b64}"
                except Exception:
                    pass

            return {
                "is_web": True,
                "protocol": proto,
                "title": title,
                "favicon": favicon,
                "url": url
            }
        except Exception:
            continue

    return {
        "is_web": False,
        "protocol": "tcp",
        "title": "",
        "favicon": "",
        "url": ""
    }


@app.get("/api/scan-nas")
async def scan_nas(
    request: Request,
    ip: str = Query("", description="Target IP to scan"),
    mode: str = Query("core", description="Scan mode: core, range, custom"),
    ports: str = Query("", description="Custom port list or range, e.g. 1-10000"),
    _auth: dict = Depends(require_auth)
):
    target_ip = (ip or "").strip()
    if not target_ip:
        target_ip = db.get_config("nas_ip", "")
    if not target_ip:
        client_host = request.headers.get("host", "").split(":")[0]
        target_ip = client_host if client_host and client_host not in ("localhost", "0.0.0.0") else "127.0.0.1"

    if target_ip and target_ip != "127.0.0.1":
        db.set_config("nas_ip", target_ip)

    if mode in ("range", "range10k") or ports:
        port_spec = ports or ("1-10000" if mode == "range10k" else "1-10000")
        ports_to_scan = parse_port_specs(port_spec)
        if not ports_to_scan:
            ports_to_scan = CORE_SCAN_PORTS
    else:
        ports_to_scan = CORE_SCAN_PORTS

    sem = asyncio.Semaphore(150)
    tasks = [_probe_port(target_ip, p, sem) for p in ports_to_scan]
    results = await asyncio.gather(*tasks)
    open_ports = [p for p in results if p is not None]

    existing_ports = db.get_service_ports()
    all_services = {s["port"]: s for s in db.get_all_services() if s.get("port")}

    web_tasks = [_probe_web_service(target_ip, p, client) for p in open_ports]
    web_results = await asyncio.gather(*web_tasks)

    discovered = []
    existing = []

    for port, web_info in zip(open_ports, web_results):
        known_info = KNOWN_SERVICES.get(port, (f"端口 {port} 服务", "🔧", "tcp"))
        known_name, known_icon, known_proto = known_info

        is_web = web_info["is_web"]
        proto = web_info["protocol"] if is_web else known_proto
        title = web_info["title"] or known_name
        favicon = web_info["favicon"]
        url = web_info["url"] if is_web else (f"{proto}://{target_ip}:{port}" if proto in ("http", "https") else "")
        icon = known_icon if not favicon else ""

        item = {
            "port": port,
            "name": title,
            "icon": icon or ("🌐" if is_web else "🔧"),
            "protocol": proto,
            "favicon": favicon,
            "url": url,
            "is_web": is_web,
            "already_exists": port in existing_ports
        }

        if port in existing_ports:
            existing_svc = all_services.get(port, {})
            item["existing_name"] = existing_svc.get("name", title)
            existing.append(item)
        else:
            discovered.append(item)

    return {
        "ok": True,
        "nas_ip": target_ip,
        "scanned_count": len(ports_to_scan),
        "open_ports": open_ports,
        "new_services": discovered,
        "existing_services": existing,
    }


@app.post("/api/fetch-meta")
async def fetch_meta(request: Request, _auth: dict = Depends(require_auth)):
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return JSONResponse({"error": "缺少 URL 地址"}, status_code=400)
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    url = validate_proxy_url(url, allow_private=True)

    http_client = client if (client and not client.is_closed) else httpx.AsyncClient(timeout=8, verify=False, follow_redirects=True)

    try:
        parsed = urlparse(url)
        port = parsed.port
        if not port:
            port = 443 if parsed.scheme == "https" else 80
        protocol = parsed.scheme

        resp = await http_client.get(url, follow_redirects=True, timeout=8)
        html = resp.text[:50000]

        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if m:
            title = html_lib.unescape(m.group(1).strip())
        if not title:
            title = parsed.hostname or ""

        favicon_url = ""
        for pat in [
            r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]+href=["\']([^"\']+)["\']',
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'](?:shortcut )?icon["\']',
            r'<link[^>]+rel=["\']apple-touch-icon["\'][^>]+href=["\']([^"\']+)["\']',
            r'<link[^>]+rel=["\']icon["\'][^>]+href=["\']([^"\']+)["\']',
        ]:
            fm = re.search(pat, html, re.IGNORECASE)
            if fm:
                favicon_url = fm.group(1)
                break

        if not favicon_url:
            favicon_url = "/favicon.ico"

        icon_data = ""
        if favicon_url.startswith("data:"):
            icon_data = favicon_url
        else:
            abs_icon = urljoin(url, favicon_url)
            try:
                ir = await http_client.get(abs_icon, follow_redirects=True, timeout=5)
                if ir.status_code == 200:
                    ct = ir.headers.get("content-type", "image/x-icon")
                    b64 = base64.b64encode(ir.content).decode()
                    icon_data = f"data:{ct};base64,{b64}"
            except Exception:
                pass

        return {"ok": True, "title": title, "icon": icon_data, "port": port, "protocol": protocol}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/load-icon-url")
async def load_icon_url(request: Request, _auth: dict = Depends(require_auth)):
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return JSONResponse({"error": "请输入图标 URL"}, status_code=400)
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    url = validate_proxy_url(url, allow_private=False)

    http_client = client if (client and not client.is_closed) else httpx.AsyncClient(timeout=8, verify=False, follow_redirects=True)

    try:
        resp = await http_client.get(url, follow_redirects=True, timeout=8)
        if resp.status_code != 200:
            return JSONResponse({"error": f"获取失败 (HTTP {resp.status_code})"}, status_code=502)
        ct = resp.headers.get("content-type", "image/png")
        if not ct.startswith("image/"):
            return JSONResponse({"error": "只能加载图片资源"}, status_code=400)
        # Limit icon size to 5MB
        if len(resp.content) > 5 * 1024 * 1024:
            return JSONResponse({"error": "图片尺寸过大"}, status_code=413)
        b64 = base64.b64encode(resp.content).decode()
        return {"ok": True, "icon": f"data:{ct};base64,{b64}"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    p = db.get_config("port", 8980)
    uvicorn.run(app, host="0.0.0.0", port=p)
