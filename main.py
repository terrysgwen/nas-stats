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
import uuid

import logging
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
import httpx

logger = logging.getLogger("main")

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
    client = httpx.AsyncClient(timeout=10, follow_redirects=True)
    for m in active_monitors.values():
        m.set_client(client)
    reload_monitors()
    yield
    if client and not client.is_closed:
        await client.aclose()


app = FastAPI(title="NAS Stats 2.0 API", lifespan=lifespan)

# Setup CORS from DB config
cors_origins = db.get_config("cors_origins", ["*"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pages ──────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/nvr")
async def nvr_page():
    return FileResponse(STATIC_DIR / "nvr.html", media_type="text/html")


# ── Health & Stats ──────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


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
async def update_monitor_endpoint(monitor_id: str, request: Request):
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
async def test_monitor(monitor_id: str):
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
async def update_config_endpoint(request: Request):
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
from auth_manager import auth_mgr


@app.get("/api/auth/status")
async def auth_status_endpoint():
    """Get web authentication configuration and status."""
    return {"ok": True, **auth_mgr.get_status()}


@app.post("/api/auth/login")
async def auth_login_endpoint(request: Request):
    """Authenticate user credentials and generate persistent token."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    remember = bool(body.get("remember", True))

    if not auth_mgr.verify_credentials(username, password):
        return JSONResponse({"ok": False, "error": "用户名或密码错误，请重试"}, status_code=401)

    token = auth_mgr.create_token(username or "admin", remember=remember)
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
    else:
        token = request.query_params.get("token", "").strip()

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
async def auth_change_password_endpoint(request: Request):
    """Change web dashboard password."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    old_pwd = body.get("old_password", "").strip()
    new_pwd = body.get("new_password", "").strip()
    ok, msg = auth_mgr.change_password(old_pwd, new_pwd)
    if ok:
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
async def update_music_config_endpoint(request: Request):
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
async def get_music_songs():
    """Discover and return playable music tracks from music_db and fnOS."""
    host = music_db.get_music_config("fnmusic_host", db.get_config("nas_ip", "127.0.0.1"))
    port = music_db.get_music_config("fnmusic_port", 5888)
    songs = music_db.get_playlist()

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
        "fnos_music_url": f"http://{host}:{port}/music/"
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
async def add_music_song(request: Request):
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
async def delete_music_song(song_id: str):
    """Delete a song from music.db."""
    music_db.delete_song(song_id)
    return {"ok": True}


@app.post("/api/music/reset")
async def reset_music_songs():
    """Reset music playlist to default tracks."""
    music_db.reset_playlist()
    return {"ok": True}


@app.get("/api/music/fnos/stream")
async def fnos_music_stream(guid: str, request: Request):
    """Stream audio directly from fnOS Music backend with Range support."""
    if not guid:
        return JSONResponse({"error": "missing guid"}, status_code=400)

    await fnmusic_client.ensure_token()
    token = fnmusic_client.token

    target_path = "/music/api/v1/track/stream"
    params = {"guid": guid}
    authx = fnmusic_client._sign_authx("GET", target_path, params=params)
    url = f"{fnmusic_client.base_url}{target_path}"

    rng = request.headers.get("range", "none")
    music_logger.log_info(f"🎧 收到音频流请求: guid={guid[:12]}..., Range={rng}")

    req_headers = {
        "authx": authx,
        "Cookie": f"music-token={token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    if "range" in request.headers:
        req_headers["range"] = request.headers["range"]

    try:
        stream_timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=None)
        client = httpx.AsyncClient(timeout=stream_timeout, trust_env=False)
        upstream_req = client.build_request("GET", url, params=params, headers=req_headers)
        resp = await client.send(upstream_req, stream=True)

        ct = resp.headers.get("content-type", "")
        # If upstream returned JSON or error status (e.g. 100004 forbidden / missing track file)
        if "application/json" in ct or resp.status_code >= 400:
            content = await resp.aread()
            await resp.aclose()
            await client.aclose()
            music_logger.log_warn(f"⚠️ 音频流请求异常: guid={guid[:12]}..., HTTP {resp.status_code}, 内容: {content.decode('utf-8', errors='ignore')[:100]}")
            return Response(content=content, status_code=403 if resp.status_code == 200 else resp.status_code, media_type="application/json")

        cl = resp.headers.get("content-length", "未知")
        music_logger.log_info(f"▶️ 开始传输音频流: guid={guid[:12]}..., HTTP {resp.status_code}, 格式: {ct}, 大小: {cl}")

        resp_headers = {"accept-ranges": "bytes"}
        for h in ("content-length", "content-range", "content-disposition"):
            if h in resp.headers:
                resp_headers[h] = resp.headers[h]

        async def stream_generator():
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=ct or "audio/mpeg"
        )
    except Exception as e:
        music_logger.log_error(f"❌ 音频流传输异常: guid={guid[:12]}..., 错误: {e}")
        logger.error("fnos stream proxy error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/music/fnos/cover")
async def fnos_music_cover(coverId: str):
    """Proxy cover images from fnOS Music."""
    if not coverId:
        return JSONResponse({"error": "missing coverId"}, status_code=400)

    await fnmusic_client.ensure_token()
    token = fnmusic_client.token

    target_path = "/music/api/v1/static/cover"
    params = {"coverId": coverId}
    authx = fnmusic_client._sign_authx("GET", target_path, params=params)
    url = f"{fnmusic_client.base_url}{target_path}"

    req_headers = {
        "authx": authx,
        "Cookie": f"music-token={token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as cov_client:
            resp = await cov_client.get(url, params=params, headers=req_headers, follow_redirects=True)
            if resp.status_code == 200:
                ct = resp.headers.get("content-type", "image/webp")
                return Response(content=resp.content, media_type=ct)
            return JSONResponse({"error": "cover not found"}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ── Music Logs API ──────────────────────────────────────────────────

@app.get("/api/music/logs")
async def get_music_logs(limit: int = 150, level: str = "ALL", keyword: str = ""):
    """Retrieve filtered logs from data/music.log buffer."""
    logs = music_logger.get_logs(limit=limit, level=level, keyword=keyword)
    return {
        "ok": True,
        "total": len(logs),
        "logs": logs
    }


@app.post("/api/music/logs/clear")
async def clear_music_logs():
    """Clear music logs."""
    ok = music_logger.clear_logs()
    return {"ok": ok, "message": "音乐运行日志已清空" if ok else "清空失败"}


@app.get("/api/music/logs/download")
async def download_music_logs():
    """Download data/music.log directly."""
    if not music_logger.MUSIC_LOG_FILE.exists():
        with open(music_logger.MUSIC_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
    return FileResponse(
        path=music_logger.MUSIC_LOG_FILE,
        filename="music.log",
        media_type="text/plain; charset=utf-8"
    )


@app.get("/api/music/stream-proxy")
async def music_stream_proxy(request: Request, url: str = ""):
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
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
    resp = await client.get(
        "https://www.bing.com/HPImageArchive.aspx",
        params={"format": "js", "idx": "0", "n": "8", "mkt": "zh-CN"},
        follow_redirects=True, timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()
    images = []
    for img in data.get("images", []):
        url_path = img.get("url", "")
        if not url_path:
            continue
        full_url = "https://www.bing.com" + url_path
        images.append({
            "url": "/api/bg-proxy?url=" + full_url,
            "title": img.get("copyright", ""),
            "date": img.get("startdate", ""),
        })
    if not images:
        raise ValueError("Bing returned no images")
    return images


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
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    try:
        resp = await client.get(url, follow_redirects=True, timeout=10)
        ct = resp.headers.get("content-type", "image/jpeg")
        return Response(content=resp.content, media_type=ct)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ── Services Management (CRUD) ──────────────────────────────────────

@app.api_route("/api/services", methods=["GET", "POST"])
async def handle_services(request: Request, action: str = Query(...)):
    if action == "list":
        services = db.get_all_services()
        return {"items": services}

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

SCAN_PORTS = [
    21, 22, 23, 25, 53, 80, 110, 135, 139, 143, 443, 445, 548, 631,
    993, 995, 1080, 1433, 1521, 1880, 1883, 2049, 2375, 2376, 3000,
    3001, 3242, 3306, 3389, 4040, 4443, 5000, 5001, 5005, 5080, 5432,
    5900, 5901, 6598, 6690, 6767, 6881, 7878, 8000, 8008, 8010, 8020,
    8080, 8081, 8082, 8083, 8084, 8085, 8086, 8087, 8088, 8089, 8090,
    8091, 8095, 8096, 8112, 8123, 8181, 8200, 8222, 8334, 8384, 8443,
    8448, 8554, 8787, 8880, 8888, 8920, 8980, 8989, 9000, 9002, 9090,
    9091, 9117, 9443, 9696, 9980, 9999, 10000, 10101, 14000, 32400,
    49152, 49153, 49154, 51413, 51414, 51415,
]

KNOWN_SERVICES = {
    21: ("FTP 服务", "📁", "tcp"), 22: ("SSH", "🔒", "tcp"),
    23: ("Telnet", "🖥️", "tcp"), 25: ("SMTP 邮件", "📧", "tcp"),
    53: ("DNS", "🌐", "tcp"), 80: ("Web 服务", "🌐", "http"),
    110: ("POP3 邮件", "📧", "tcp"), 135: ("RPC", "🖥️", "tcp"),
    139: ("NetBIOS", "🖥️", "tcp"), 143: ("IMAP 邮件", "📧", "tcp"),
    443: ("HTTPS 服务", "🔒", "https"), 445: ("SMB 文件共享", "📂", "tcp"),
    548: ("AFP 文件共享", "📂", "tcp"), 631: ("打印服务", "🖨️", "http"),
    993: ("IMAPS 邮件", "📧", "tcp"), 995: ("POP3S 邮件", "📧", "tcp"),
    1880: ("Node-RED", "🔧", "http"), 1883: ("MQTT", "📡", "tcp"),
    2049: ("NFS 文件共享", "📂", "tcp"), 2375: ("Docker", "🐳", "http"),
    2376: ("Docker TLS", "🐳", "tcp"), 3000: ("Web 应用", "🌐", "http"),
    3306: ("MySQL 数据库", "🗄️", "tcp"), 3389: ("远程桌面 RDP", "🖥️", "tcp"),
    4040: ("Web 应用", "🌐", "http"), 5000: ("Web 应用", "🌐", "http"),
    5001: ("Web 应用", "🌐", "https"), 5005: ("Web 应用", "🌐", "http"),
    5080: ("Web 应用", "🌐", "http"), 5432: ("PostgreSQL 数据库", "🗄️", "tcp"),
    5900: ("VNC 远程", "🖥️", "tcp"), 5901: ("VNC 远程", "🖥️", "tcp"),
    6767: ("BaiduPCS", "📁", "http"), 6881: ("BitTorrent", "📥", "tcp"),
    7878: ("Sonarr", "📺", "http"), 8000: ("Web 应用", "🌐", "http"),
    8008: ("Web 应用", "🌐", "http"), 8080: ("Web 代理", "🌐", "http"),
    8081: ("Web 应用", "🌐", "http"), 8082: ("Web 应用", "🌐", "http"),
    8083: ("Web 应用", "🌐", "https"), 8085: ("qBittorrent", "📥", "http"),
    8086: ("Web 应用", "🌐", "http"), 8087: ("Web 应用", "🌐", "http"),
    8088: ("Web 应用", "🌐", "http"), 8089: ("Web 应用", "🌐", "http"),
    8090: ("Web 应用", "🌐", "http"), 8091: ("Web 应用", "🌐", "http"),
    8112: ("Deluge", "📥", "http"), 8123: ("Home Assistant", "🏠", "http"),
    8181: ("Web 应用", "🌐", "http"), 8200: ("Web 应用", "🌐", "http"),
    8222: ("Web 应用", "🌐", "http"), 8384: ("Syncthing", "🔄", "http"),
    8443: ("HTTPS 应用", "🔒", "https"), 8554: ("RTSP 流媒体", "🎬", "tcp"),
    8787: ("Web 应用", "🌐", "http"), 8888: ("Web 应用", "🌐", "http"),
    8920: ("Web 应用", "🌐", "http"), 8980: ("NAS 面板", "🖥️", "http"),
    8989: ("Web 应用", "🌐", "http"), 9000: ("Web 应用", "🌐", "http"),
    9090: ("Web 应用", "🌐", "http"), 9091: ("Transmission", "📥", "http"),
    9117: ("Prowlarr", "🔍", "http"), 9443: ("HTTPS 应用", "🔒", "https"),
    9696: ("Prowlarr", "🔍", "http"), 9980: ("TVHeadend", "📺", "http"),
    9999: ("Web 应用", "🌐", "http"), 10000: ("EasyNVR / Webmin", "📹", "http"),
    32400: ("Plex", "🎬", "http"), 51413: ("Transmission", "📥", "tcp"),
}

HTTP_PROBE_PORTS = {
    80, 443, 631, 1880, 2375, 3000, 3001, 4040, 4443, 5000, 5001, 5005,
    5080, 6767, 7878, 8000, 8008, 8010, 8020, 8080, 8081, 8082, 8083,
    8084, 8085, 8086, 8087, 8088, 8089, 8090, 8091, 8095, 8096, 8112,
    8123, 8181, 8200, 8222, 8384, 8443, 8448, 8787, 8880, 8888, 8920,
    8980, 8989, 9000, 9002, 9090, 9091, 9117, 9443, 9696, 9980, 9999,
    10000, 32400,
}


async def _probe_port(ip, port):
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=1.5)
        w.close()
        await w.wait_closed()
        return True
    except Exception:
        return False


async def _probe_http_title(ip, port, protocol):
    url = f"{protocol}://{ip}:{port}"
    try:
        resp = await client.get(url, follow_redirects=True, timeout=4)
        html = resp.text[:30000]
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if m:
            title = m.group(1).strip()
        favicon_url = ""
        for pat in [
            r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]+href=["\']([^"\']+)["\']',
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'](?:shortcut )?icon["\']',
            r'<link[^>]+rel=["\']apple-touch-icon["\'][^>]+href=["\']([^"\']+)["\']',
        ]:
            fm = re.search(pat, html, re.IGNORECASE)
            if fm:
                favicon_url = fm.group(1)
                break
        favicon = ""
        if favicon_url:
            if favicon_url.startswith("data:"):
                favicon = favicon_url
            else:
                abs_icon = urljoin(url, favicon_url)
                try:
                    ir = await client.get(abs_icon, follow_redirects=True, timeout=3)
                    if ir.status_code == 200:
                        ct = ir.headers.get("content-type", "image/x-icon")
                        b64 = base64.b64encode(ir.content).decode()
                        favicon = f"data:{ct};base64,{b64}"
                except Exception:
                    pass
        return {"title": title, "favicon": favicon}
    except Exception:
        return {"title": "", "favicon": ""}


@app.get("/api/scan-nas")
async def scan_nas():
    nas_ip = db.get_config("nas_ip", "127.0.0.1")
    tasks = [_probe_port(nas_ip, p) for p in SCAN_PORTS]
    results = await asyncio.gather(*tasks)
    open_ports = [p for p, ok in zip(SCAN_PORTS, results) if ok]

    existing_ports = db.get_service_ports()
    discovered = []
    for port in sorted(open_ports):
        if port in existing_ports:
            continue
        info = KNOWN_SERVICES.get(port, (f"端口 {port} 服务", "🔧", "tcp"))
        name, icon, default_proto = info
        proto = default_proto
        if port in HTTP_PROBE_PORTS:
            proto = "https" if port in (443, 5001, 8083, 8443, 9443) else "http"

        item = {
            "port": port, "name": name, "icon": icon, "protocol": proto,
            "favicon": "", "url": f"{proto}://{nas_ip}:{port}" if proto in ("http", "https") else ""
        }
        if port in HTTP_PROBE_PORTS:
            meta = await _probe_http_title(nas_ip, port, proto)
            if meta["title"]:
                item["name"] = meta["title"]
            item["favicon"] = meta["favicon"]
        discovered.append(item)

    return {"ok": True, "nas_ip": nas_ip, "open_ports": open_ports, "new_services": discovered}


@app.post("/api/fetch-meta")
async def fetch_meta(request: Request):
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return JSONResponse({"error": "需要 URL"}, status_code=400)
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    try:
        parsed = urlparse(url)
        port = parsed.port
        protocol = parsed.scheme

        resp = await client.get(url, follow_redirects=True, timeout=8)
        html = resp.text[:50000]

        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if m:
            title = m.group(1).strip()

        favicon_url = ""
        for pat in [
            r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]+href=["\']([^"\']+)["\']',
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'](?:shortcut )?icon["\']',
            r'<link[^>]+rel=["\']apple-touch-icon["\'][^>]+href=["\']([^"\']+)["\']',
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
                ir = await client.get(abs_icon, follow_redirects=True, timeout=5)
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
async def load_icon_url(request: Request):
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return JSONResponse({"error": "请输入图标 URL"}, status_code=400)
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        resp = await client.get(url, follow_redirects=True, timeout=8)
        if resp.status_code != 200:
            return JSONResponse({"error": f"获取失败 (HTTP {resp.status_code})"}, status_code=502)
        ct = resp.headers.get("content-type", "image/png")
        if not ct.startswith("image/"):
            ct = "image/png"
        b64 = base64.b64encode(resp.content).decode()
        return {"ok": True, "icon": f"data:{ct};base64,{b64}"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    p = db.get_config("port", 8980)
    uvicorn.run(app, host="0.0.0.0", port=p)
