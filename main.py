import json
import asyncio
import time
import uuid
import base64
import hashlib
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urljoin
import re

import httpx
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

CONFIG_PATH = Path(__file__).parent / "config.json"
STATIC_DIR = Path(__file__).parent / "static"
DATA_DIR = Path(__file__).parent / "data"
SERVICES_PATH = DATA_DIR / "services.json"

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

cache = {"data": None, "ts": 0}
CACHE_TTL = 3

qb_client = None
qb_logged_in = False

client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, qb_client, ikuai_client
    client = httpx.AsyncClient(timeout=10)
    qb_client = httpx.AsyncClient(timeout=10, follow_redirects=True)
    ikuai_client = httpx.AsyncClient(timeout=10, follow_redirects=True)
    yield
    await client.aclose()
    await qb_client.aclose()
    await ikuai_client.aclose()


app = FastAPI(title="NAS Stats API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.get("cors_origins", ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _qb_login() -> bool:
    global qb_logged_in
    svc = config["services"]["qbittorrent"]
    try:
        resp = await qb_client.post(
            f"{svc['url']}/api/v2/auth/login",
            data={"username": svc["username"], "password": svc["password"]},
        )
        qb_logged_in = resp.text.strip() == "Ok."
        if not qb_logged_in:
            print(f"[qb] login failed: {resp.status_code} {resp.text[:100]}")
        return qb_logged_in
    except Exception as e:
        print(f"[qb] login error: {e}")
        qb_logged_in = False
        return False


async def fetch_qbittorrent() -> dict:
    global qb_logged_in
    svc = config["services"]["qbittorrent"]
    if not svc.get("enabled"):
        return {"enabled": False, "status": "disabled"}

    for attempt in range(2):
        try:
            if not qb_logged_in:
                if not await _qb_login():
                    return {"enabled": True, "status": "auth_failed"}

            base = svc["url"]
            transfer_resp = await qb_client.get(f"{base}/api/v2/transfer/info")
            torrents_resp = await qb_client.get(f"{base}/api/v2/torrents/info")

            if transfer_resp.status_code == 403 or torrents_resp.status_code == 403:
                print(f"[qb] 403 forbidden, re-authenticating (attempt {attempt + 1})")
                qb_logged_in = False
                qb_client.cookies.clear()
                continue

            if transfer_resp.status_code != 200:
                print(f"[qb] transfer/info returned {transfer_resp.status_code}")
                qb_logged_in = False
                qb_client.cookies.clear()
                continue

            transfer = transfer_resp.json()
            torrents = torrents_resp.json()

            downloading = sum(1 for t in torrents if t.get("state") in ("downloading", "stalledDL", "metaDL", "forcedMetaDL"))
            seeding = sum(1 for t in torrents if t.get("state") in ("uploading", "stalledUP", "forcedUP"))
            paused = sum(1 for t in torrents if t.get("state") in ("pausedDL", "pausedUP"))
            errored = sum(1 for t in torrents if t.get("state") in ("error", "missingFiles"))

            return {
                "enabled": True,
                "status": "ok",
                "dl_speed": transfer.get("dl_info_speed", 0),
                "up_speed": transfer.get("up_info_speed", 0),
                "dl_total": transfer.get("dl_info_data", 0),
                "up_total": transfer.get("up_info_data", 0),
                "connection": transfer.get("connection_status", "disconnected"),
                "total": len(torrents),
                "downloading": downloading,
                "seeding": seeding,
                "paused": paused,
                "errored": errored,
            }
        except Exception as e:
            if attempt == 0:
                qb_logged_in = False
                qb_client.cookies.clear()
                continue
            return {"enabled": True, "status": "error", "error": str(e)}
    return {"enabled": True, "status": "auth_failed"}


async def fetch_transmission() -> dict:
    svc = config["services"]["transmission"]
    if not svc.get("enabled"):
        return {"enabled": False, "status": "disabled"}
    try:
        auth = None
        if svc.get("username"):
            auth = (svc["username"], svc.get("password", ""))

        headers = {}
        session_id = ""

        for attempt in range(2):
            body = {
                "method": "session-stats",
                "arguments": {},
            }
            resp = await client.post(
                f"{svc['url']}/transmission/rpc",
                json=body,
                auth=auth,
                headers=headers,
            )
            if resp.status_code == 409:
                session_id = resp.headers.get("X-Transmission-Session-Id", "")
                headers["X-Transmission-Session-Id"] = session_id
                continue

            stats = resp.json().get("arguments", {})

            body2 = {
                "method": "torrent-get",
                "arguments": {
                    "fields": ["status", "rateDownload", "rateUpload", "percentDone", "error", "errorString"],
                },
            }
            resp2 = await client.post(
                f"{svc['url']}/transmission/rpc",
                json=body2,
                auth=auth,
                headers=headers,
            )
            torrents = resp2.json().get("arguments", {}).get("torrents", [])

            downloading = sum(1 for t in torrents if t.get("status") == 4)
            seeding = sum(1 for t in torrents if t.get("status") == 8)
            paused = sum(1 for t in torrents if t.get("status") in (0, 1, 2))
            errored = sum(1 for t in torrents if t.get("error", 0) > 0)

            return {
                "enabled": True,
                "status": "ok",
                "dl_speed": stats.get("downloadSpeed", 0),
                "up_speed": stats.get("uploadSpeed", 0),
                "active": stats.get("activeTorrentCount", 0),
                "paused_count": stats.get("pausedTorrentCount", 0),
                "total": stats.get("torrentCount", 0),
                "downloading": downloading,
                "seeding": seeding,
                "paused": paused,
                "errored": errored,
                "cumulative": stats.get("cumulative-stats", {}),
            }

        return {"enabled": True, "status": "error", "error": "CSRF retry failed"}
    except Exception as e:
        return {"enabled": True, "status": "error", "error": str(e)}


ikuai_client = None
ikuai_logged_in = False


async def _ikuai_login() -> bool:
    global ikuai_logged_in
    svc = config["services"].get("ikuai")
    if not svc:
        return False
    try:
        passwd_md5 = hashlib.md5(svc["password"].encode()).hexdigest()
        pass_encoded = base64.b64encode(f"salt_11{passwd_md5}".encode()).decode()
        resp = await ikuai_client.post(
            f"{svc['url']}/Action/login",
            json={"username": svc["username"], "passwd": passwd_md5, "pass": pass_encoded, "remember_password": ""},
        )
        data = resp.json()
        print(f"[ikuai] login resp: {resp.status_code} {resp.text[:200]}")
        print(f"[ikuai] client cookies: {dict(ikuai_client.cookies)}")
        ikuai_logged_in = data.get("Data", {}).get("login") == 1 or resp.cookies.get("sess_key") is not None
        if not ikuai_logged_in:
            print(f"[ikuai] login failed: {resp.status_code} {resp.text[:100]}")
        return ikuai_logged_in
    except Exception as e:
        print(f"[ikuai] login error: {e}")
        ikuai_logged_in = False
        return False


async def _ikuai_call(func_name: str, action: str = "", param: dict = None) -> dict:
    svc = config["services"].get("ikuai")
    if not svc:
        return {}
    payload = {"func_name": func_name, "action": action}
    if param:
        payload["param"] = param
    try:
        resp = await ikuai_client.post(
            f"{svc['url']}/Action/call",
            json=payload,
        )
        return resp.json()
    except Exception as e:
        print(f"[ikuai] call {func_name} error: {e}")
        return {}


async def fetch_ikuai() -> dict:
    global ikuai_logged_in
    svc = config["services"].get("ikuai")
    if not svc or not svc.get("enabled"):
        return {"enabled": False, "status": "disabled"}

    for attempt in range(2):
        try:
            if not ikuai_logged_in:
                if not await _ikuai_login():
                    return {"enabled": True, "status": "auth_failed"}

            sysstat = await _ikuai_call("sysstat", "show", {"TYPE": "verinfo,cpu,memory,stream,cputemp"})
            wan_resp = await _ikuai_call("wan", "show", {})

            print(f"[ikuai] sysstat: {json.dumps(sysstat, ensure_ascii=False)[:1500]}")
            print(f"[ikuai] wan: {json.dumps(wan_resp, ensure_ascii=False)[:1500]}")

            if not sysstat and not wan_resp:
                ikuai_logged_in = False
                ikuai_client.cookies.clear()
                continue

            result = {"enabled": True, "status": "ok"}
            r = (sysstat.get("results") or sysstat.get("Data") or {}) if sysstat else {}

            if r:
                if "cpu" in r:
                    cpus = r["cpu"]
                    if isinstance(cpus, list):
                        try:
                            result["cpu"] = round(sum(float(str(c).replace("%", "")) for c in cpus) / len(cpus), 1)
                        except (ValueError, ZeroDivisionError):
                            result["cpu"] = 0
                    else:
                        result["cpu"] = cpus
                if "memory" in r:
                    mem = r["memory"]
                    if isinstance(mem, dict) and mem.get("total"):
                        used = mem["total"] - mem.get("available", mem.get("free", 0))
                        result["mem"] = round(used / mem["total"] * 100, 1)
                if "stream" in r:
                    stream = r["stream"]
                    if isinstance(stream, dict):
                        result["dl_speed"] = stream.get("download", 0)
                        result["up_speed"] = stream.get("upload", 0)
                        result["dl_total"] = stream.get("total_down", 0)
                        result["up_total"] = stream.get("total_up", 0)
                        result["connect_num"] = stream.get("connect_num", 0)

            wan_data = (wan_resp.get("results") or {}).get("data") if wan_resp else None
            if isinstance(wan_data, list) and wan_data:
                wans = []
                for w in wan_data:
                    if isinstance(w, dict):
                        wans.append({
                            "name": w.get("name", ""),
                            "ip": w.get("ip", w.get("dhcp_gateway", "")),
                            "dl_speed": w.get("download", 0),
                            "up_speed": w.get("upload", 0),
                        })
                result["wans"] = wans

            result.setdefault("wan_ip", "")
            result.setdefault("dl_speed", 0)
            result.setdefault("up_speed", 0)

            return result
        except Exception as e:
            if attempt == 0:
                ikuai_logged_in = False
                ikuai_client.cookies.clear()
                continue
            return {"enabled": True, "status": "error", "error": str(e)}
    return {"enabled": True, "status": "auth_failed"}


@app.get("/api/stats")
async def get_stats():
    now = time.time()
    if cache["data"] and now - cache["ts"] < CACHE_TTL:
        return cache["data"]

    qb_task = fetch_qbittorrent()
    tr_task = fetch_transmission()
    ik_task = fetch_ikuai()

    qb, tr, ik = await asyncio.gather(qb_task, tr_task, ik_task)

    result = {
        "qbittorrent": qb,
        "transmission": tr,
        "ikuai": ik,
        "timestamp": int(now),
    }
    cache["data"] = result
    cache["ts"] = now
    return result


@app.get("/api/health")
async def health():
    return {"status": "ok"}


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
            "title": "Random Photo",
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
            print(f"[bg] loaded {len(images)} images from {source_name}")
            return result
        except Exception as e:
            print(f"[bg] {source_name} failed: {e}")

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
    9999: ("Web 应用", "🌐", "http"), 10000: ("Webmin", "🛠️", "http"),
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
    cfg = load_config()
    nas_ip = cfg.get("nas_ip", "YOUR_NAS_IP")
    tasks = [_probe_port(nas_ip, p) for p in SCAN_PORTS]
    results = await asyncio.gather(*tasks)
    open_ports = [p for p, ok in zip(SCAN_PORTS, results) if ok]

    services = _load_services()
    existing_ports = {(s.get("port"), s.get("protocol", "http")) for s in services}

    discovered = []
    for port in sorted(open_ports):
        info = KNOWN_SERVICES.get(port, (f"端口 {port} 服务", "🔧", "tcp"))
        name, icon, default_proto = info
        proto = default_proto
        if port in HTTP_PROBE_PORTS:
            proto = "https" if port in (443, 5001, 8083, 8443, 9443) else "http"
        if (port, proto) in existing_ports:
            continue
        if (port, "http") in existing_ports or (port, "https") in existing_ports or (port, "tcp") in existing_ports:
            continue
        item = {"port": port, "name": name, "icon": icon, "protocol": proto, "favicon": "", "url": f"{proto}://{nas_ip}:{port}"}
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
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = title_match.group(1).strip()

        favicon_url = ""
        icon_patterns = [
            r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]+href=["\']([^"\']+)["\']',
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'](?:shortcut )?icon["\']',
            r'<link[^>]+rel=["\']apple-touch-icon["\'][^>]+href=["\']([^"\']+)["\']',
        ]
        for pattern in icon_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                favicon_url = match.group(1)
                break

        if not favicon_url:
            favicon_url = "/favicon.ico"

        if favicon_url.startswith("data:"):
            icon_data = favicon_url
        else:
            abs_icon = urljoin(url, favicon_url)
            try:
                icon_resp = await client.get(abs_icon, follow_redirects=True, timeout=5)
                if icon_resp.status_code == 200:
                    ct = icon_resp.headers.get("content-type", "image/x-icon")
                    b64 = base64.b64encode(icon_resp.content).decode()
                    icon_data = f"data:{ct};base64,{b64}"
                else:
                    icon_data = ""
            except Exception:
                icon_data = ""

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


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


# --- Service management ---

_services_lock = threading.Lock()

DEFAULT_SERVICES = [
  {"port": 5888, "name": "飞牛 fnOS", "category": "NAS 管理", "protocol": "http", "icon": "🐂", "description": "NAS 系统管理面板", "url": "http://YOUR_NAS_IP:5888"},
  {"port": 5889, "name": "飞牛 fnOS (SSL)", "category": "NAS 管理", "protocol": "https", "icon": "", "description": "NAS 管理面板 (HTTPS)", "url": "https://YOUR_NAS_IP:5889"},
  {"port": 9990, "name": "飞牛桌面管理", "category": "NAS 管理", "protocol": "http", "icon": "🖥️", "description": "飞牛桌面管理工具", "url": "http://YOUR_NAS_IP:9990"},
  {"port": 88, "name": "Heimdall", "category": "NAS 管理", "protocol": "http", "icon": "🏠", "description": "NAS 应用导航仪表盘", "url": "http://YOUR_NAS_IP:88"},
  {"port": 444, "name": "Heimdall (SSL)", "category": "NAS 管理", "protocol": "https", "icon": "", "description": "NAS 应用导航 (HTTPS)", "url": "https://YOUR_NAS_IP:444"},
  {"port": 10789, "name": "BestNav", "category": "NAS 管理", "protocol": "http", "icon": "🧭", "description": "飞牛NAS导航页", "url": "http://YOUR_NAS_IP:10789"},
  {"port": 2829, "name": "Nginx 默认", "category": "NAS 管理", "protocol": "http", "icon": "", "description": "Nginx 欢迎页", "url": "http://YOUR_NAS_IP:2829"},
  {"port": 3000, "name": "MoviePilot", "category": "影视媒体", "protocol": "http", "icon": "🎥", "description": "影视自动化管理工具", "url": "http://YOUR_NAS_IP:3000"},
  {"port": 8096, "name": "Emby", "category": "影视媒体", "protocol": "http", "icon": "📺", "description": "媒体服务器", "url": "http://YOUR_NAS_IP:8096"},
  {"port": 32400, "name": "Plex", "category": "影视媒体", "protocol": "http", "icon": "▶️", "description": "流媒体播放器 (需认证)", "url": "http://YOUR_NAS_IP:32400"},
  {"port": 8005, "name": "飞牛影视", "category": "影视媒体", "protocol": "http", "icon": "🎞️", "description": "飞牛原生影视应用", "url": "http://YOUR_NAS_IP:8005"},
  {"port": 3009, "name": "清和电视", "category": "影视媒体", "protocol": "http", "icon": "📡", "description": "在线电视直播", "url": "http://YOUR_NAS_IP:3009"},
  {"port": 3029, "name": "MoonTV", "category": "影视媒体", "protocol": "http", "icon": "🌙", "description": "在线影视播放平台", "url": "http://YOUR_NAS_IP:3029"},
  {"port": 8787, "name": "银河JAV", "category": "影视媒体", "protocol": "http", "icon": "🌌", "description": "GalaxyJAV 媒体管理", "url": "http://YOUR_NAS_IP:8787"},
  {"port": 45849, "name": "Go2RTC", "category": "影视媒体", "protocol": "http", "icon": "📹", "description": "WebRTC 视频流转发", "url": "http://YOUR_NAS_IP:45849"},
  {"port": 3024, "name": "IPTV 控制中心", "category": "影视媒体", "protocol": "http", "icon": "📺", "description": "IPTV 直播源管理", "url": "http://YOUR_NAS_IP:3024"},
  {"port": 8689, "name": "Leelaa Playlist", "category": "影视媒体", "protocol": "http", "icon": "📋", "description": "播放列表管理", "url": "http://YOUR_NAS_IP:8689"},
  {"port": 3020, "name": "道理鱼音乐", "category": "音乐有声", "protocol": "http", "icon": "🐟", "description": "在线音乐播放平台", "url": "http://YOUR_NAS_IP:3020"},
  {"port": 8099, "name": "SQMusic", "category": "音乐有声", "protocol": "http", "icon": "", "description": "音乐播放服务", "url": "http://YOUR_NAS_IP:8099"},
  {"port": 13378, "name": "Audiobookshelf", "category": "音乐有声", "protocol": "http", "icon": "🎧", "description": "有声书管理平台", "url": "http://YOUR_NAS_IP:13378"},
  {"port": 3019, "name": "Ting Reader", "category": "音乐有声", "protocol": "http", "icon": "🔊", "description": "自托管有声书平台", "url": "http://YOUR_NAS_IP:3019"},
  {"port": 8085, "name": "qBittorrent", "category": "下载传输", "protocol": "http", "icon": "⬇️", "description": "BT 下载客户端 Web UI", "url": "http://YOUR_NAS_IP:8085"},
  {"port": 9091, "name": "Transmission", "category": "下载传输", "protocol": "http", "icon": "", "description": "BT 下载客户端 (需认证)", "url": "http://YOUR_NAS_IP:9091"},
  {"port": 9090, "name": "hlink", "category": "下载传输", "protocol": "http", "icon": "🔗", "description": "局域网文件传输工具", "url": "http://YOUR_NAS_IP:9090"},
  {"port": 3003, "name": "天翼云盘转存", "category": "下载传输", "protocol": "http", "icon": "☁️", "description": "天翼云盘自动转存系统", "url": "http://YOUR_NAS_IP:3003"},
  {"port": 3004, "name": "Vertex", "category": "下载传输", "protocol": "http", "icon": "🔄", "description": "PT 资源自动化管理", "url": "http://YOUR_NAS_IP:3004"},
  {"port": 18787, "name": "IYUUPlus", "category": "下载传输", "protocol": "http", "icon": "📨", "description": "PT 辅助工具 (开发版)", "url": "http://YOUR_NAS_IP:18787"},
  {"port": 3006, "name": "PanSou 盘搜", "category": "下载传输", "protocol": "http", "icon": "", "description": "网盘资源搜索引擎", "url": "http://YOUR_NAS_IP:3006"},
  {"port": 9000, "name": "Portainer", "category": "开发运维", "protocol": "http", "icon": "", "description": "Docker 容器管理", "url": "http://YOUR_NAS_IP:9000"},
  {"port": 8443, "name": "Lucky", "category": "开发运维", "protocol": "https", "icon": "🍀", "description": "反向代理 / 端口转发", "url": "https://YOUR_NAS_IP:8443"},
  {"port": 7000, "name": "Lucky (HTTPS)", "category": "开发运维", "protocol": "https", "icon": "🍀", "description": "反向代理 (HTTPS)", "url": "https://YOUR_NAS_IP:7000"},
  {"port": 3011, "name": "Uptime Kuma", "category": "开发运维", "protocol": "http", "icon": "📊", "description": "服务状态监控面板", "url": "http://YOUR_NAS_IP:3011"},
  {"port": 3028, "name": "Vigil", "category": "开发运维", "protocol": "http", "icon": "", "description": "Docker 镜像监控", "url": "http://YOUR_NAS_IP:3028"},
  {"port": 3031, "name": "WorkBuddy", "category": "开发运维", "protocol": "http", "icon": "📈", "description": "用量看板", "url": "http://YOUR_NAS_IP:3031"},
  {"port": 18090, "name": "phpMyAdmin", "category": "开发运维", "protocol": "http", "icon": "🗄️", "description": "MySQL 数据库管理", "url": "http://YOUR_NAS_IP:18090"},
  {"port": 81, "name": "Nginx", "category": "开发运维", "protocol": "http", "icon": "🌐", "description": "Nginx 服务 (403)", "url": "http://YOUR_NAS_IP:81"},
  {"port": 8082, "name": "轻阅读", "category": "教育阅读", "protocol": "http", "icon": "📖", "description": "在线阅读应用", "url": "http://YOUR_NAS_IP:8082"},
  {"port": 8083, "name": "轻课堂", "category": "教育阅读", "protocol": "http", "icon": "🎓", "description": "Lite Class 教学平台", "url": "http://YOUR_NAS_IP:8083"},
  {"port": 8090, "name": "talebook", "category": "教育阅读", "protocol": "http", "icon": "", "description": "在线电子书图书馆", "url": "http://YOUR_NAS_IP:8090"},
  {"port": 9280, "name": "WizNote", "category": "教育阅读", "protocol": "http", "icon": "📝", "description": "为知笔记服务", "url": "http://YOUR_NAS_IP:9280"},
  {"port": 3012, "name": "AI 智能错题本", "category": "教育阅读", "protocol": "http", "icon": "❌", "description": "AI 驱动错题管理", "url": "http://YOUR_NAS_IP:3012"},
  {"port": 3018, "name": "字幕助手", "category": "教育阅读", "protocol": "http", "icon": "💬", "description": "影片整理与字幕下载", "url": "http://YOUR_NAS_IP:3018"},
  {"port": 3027, "name": "字幕助手 (备)", "category": "教育阅读", "protocol": "http", "icon": "💬", "description": "影片整理与字幕下载 (备用)", "url": "http://YOUR_NAS_IP:3027"},
  {"port": 5244, "name": "Alist", "category": "工具应用", "protocol": "http", "icon": "", "description": "网盘聚合文件列表", "url": "http://YOUR_NAS_IP:5244"},
  {"port": 5230, "name": "Memos", "category": "工具应用", "protocol": "http", "icon": "🗒️", "description": "自托管备忘录/笔记", "url": "http://YOUR_NAS_IP:5230"},
  {"port": 8923, "name": "QD 框架", "category": "工具应用", "protocol": "http", "icon": "✅", "description": "自动签到框架", "url": "http://YOUR_NAS_IP:8923"},
  {"port": 18230, "name": "FnMessageBot", "category": "工具应用", "protocol": "http", "icon": "🤖", "description": "飞牛消息机器人", "url": "http://YOUR_NAS_IP:18230"},
  {"port": 8901, "name": "网址收藏夹", "category": "工具应用", "protocol": "http", "icon": "🔖", "description": "个人网址书签导航", "url": "http://YOUR_NAS_IP:8901"},
  {"port": 5005, "name": "Python App", "category": "工具应用", "protocol": "http", "icon": "🐍", "description": "Werkzeug/Flask 应用", "url": "http://YOUR_NAS_IP:5005"},
  {"port": 8024, "name": "Python App (2)", "category": "工具应用", "protocol": "http", "icon": "🐍", "description": "Werkzeug/Flask 应用", "url": "http://YOUR_NAS_IP:8024"},
  {"port": 10000, "name": "视频监控", "category": "工具应用", "protocol": "http", "icon": "", "description": "EasyNVR 视频流管理", "url": "http://YOUR_NAS_IP:10000/cloud/"},
  {"port": 3001, "name": "Uvicorn API", "category": "工具应用", "protocol": "http", "icon": "⚡", "description": "FastAPI/Uvicorn 服务", "url": "http://YOUR_NAS_IP:3001"},
  {"port": 25, "name": "SMTP", "category": "基础服务", "protocol": "tcp", "icon": "📤", "description": "邮件发送服务", "url": ""},
  {"port": 110, "name": "POP3", "category": "基础服务", "protocol": "tcp", "icon": "", "description": "邮件接收服务", "url": ""},
  {"port": 143, "name": "IMAP", "category": "基础服务", "protocol": "tcp", "icon": "📬", "description": "邮件同步协议", "url": ""},
  {"port": 139, "name": "NetBIOS", "category": "基础服务", "protocol": "tcp", "icon": "📂", "description": "网络基本输入输出", "url": ""},
  {"port": 445, "name": "SMB", "category": "基础服务", "protocol": "tcp", "icon": "", "description": "Windows 文件共享", "url": ""},
  {"port": 3306, "name": "MySQL", "category": "基础服务", "protocol": "tcp", "icon": "🗄️", "description": "关系型数据库", "url": ""},
  {"port": 49152, "name": "UPnP", "category": "基础服务", "protocol": "tcp", "icon": "🔗", "description": "通用即插即用设备", "url": ""},
  {"port": 51413, "name": "Transmission 传输", "category": "基础服务", "protocol": "tcp", "icon": "⬇️", "description": "BT 传输端口", "url": ""},
]


def _load_services() -> list:
    with _services_lock:
        if not SERVICES_PATH.exists():
            return []
        with open(SERVICES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)


def _save_services(services: list):
    with _services_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(SERVICES_PATH, "w", encoding="utf-8") as f:
            json.dump(services, f, ensure_ascii=False, indent=2)


@app.api_route("/api/services", methods=["GET", "POST"])
async def handle_services(request: Request, action: str = Query(...)):
    if action == "list":
        services = _load_services()
        return {"items": services}

    body = await request.json() if request.method == "POST" else {}

    if action == "toggle":
        services = _load_services()
        for s in services:
            if s["id"] == body.get("id"):
                s["visible"] = body.get("visible", True)
                break
        _save_services(services)
        return {"ok": True}

    if action == "update":
        services = _load_services()
        for s in services:
            if s["id"] == body.get("id"):
                s["name"] = body.get("name", s["name"])
                s["icon"] = body.get("icon", s.get("icon", ""))
                s["description"] = body.get("desc", s.get("description", ""))
                s["url"] = body.get("url", s.get("url", ""))
                s["port"] = body.get("port", s["port"])
                s["protocol"] = body.get("protocol", s.get("protocol", "http"))
                s["favicon"] = body.get("favicon", s.get("favicon", ""))
                break
        _save_services(services)
        return {"ok": True}

    if action == "delete":
        services = _load_services()
        services = [s for s in services if s["id"] != body.get("id")]
        _save_services(services)
        return {"ok": True}

    if action == "add":
        services = _load_services()
        new_item = {
            "id": str(uuid.uuid4()),
            "port": body.get("port", 0),
            "name": body.get("name", ""),
            "category": body.get("category", "自定义"),
            "protocol": body.get("protocol", "http"),
            "icon": body.get("icon", "🔧"),
            "description": body.get("desc", ""),
            "url": body.get("url", ""),
            "favicon": body.get("favicon", ""),
            "visible": True,
            "sort_order": len(services),
        }
        services.append(new_item)
        _save_services(services)
        return {"ok": True, "id": new_item["id"]}

    if action == "reorder":
        services = _load_services()
        order_map = {o["id"]: o["sort_order"] for o in body.get("order", [])}
        for s in services:
            if s["id"] in order_map:
                s["sort_order"] = order_map[s["id"]]
        services.sort(key=lambda s: s["sort_order"])
        _save_services(services)
        return {"ok": True}

    if action == "import":
        services = _load_services()
        existing_ports = {s["port"] for s in services}
        added = 0
        skipped = 0
        for item in body.get("services", []):
            if item.get("port") in existing_ports:
                skipped += 1
                continue
            services.append({
                "id": str(uuid.uuid4()),
                "port": item["port"],
                "name": item.get("name", ""),
                "category": item.get("category", "自定义"),
                "protocol": item.get("protocol", "http"),
                "icon": item.get("icon", "🔧"),
                "description": item.get("desc", ""),
                "url": item.get("url", ""),
                "visible": True,
                "sort_order": len(services),
            })
            existing_ports.add(item["port"])
            added += 1
        _save_services(services)
        return {"ok": True, "added": added, "skipped": skipped}

    if action == "seed":
        services = _load_services()
        existing_ports = {s["port"] for s in services}
        added = 0
        for i, s in enumerate(DEFAULT_SERVICES):
            if s["port"] in existing_ports:
                continue
            services.append({
                "id": str(uuid.uuid4()),
                "port": s["port"],
                "name": s["name"],
                "category": s["category"],
                "protocol": s["protocol"],
                "icon": s["icon"],
                "description": s["description"],
                "url": s["url"],
                "visible": True,
                "sort_order": len(services),
            })
            existing_ports.add(s["port"])
            added += 1
        _save_services(services)
        return {"ok": True, "count": added}

    return JSONResponse({"error": "unknown action"}, status_code=400)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.get("port", 8980))
