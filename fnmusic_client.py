"""Feiniu (fnOS) Music API Client.

Directly communicates with the fnOS Music Service running on port 5888.
Handles password authentication, authx signature calculation, track discovery,
playlist retrieval, and media streaming proxying.
Logs all activities independently to data/music.log.
"""

import hashlib
import json
import random
import time
from typing import Any, Dict, List, Optional
import httpx

import music_logger
logger = music_logger.music_logger

# Constants discovered by reverse-engineering fnOS music frontend
FNOS_PREFIX = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
FNOS_API_KEY = "6D5602D4-A342-4799-A0F0-BB795E7167D0"
DEFAULT_DEVICE_ID = "0123456789abcdef0123456789abcdef"


class FnMusicClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 5888, username: str = "", password: str = ""):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.base_url = f"http://{host}:{port}"
        self.token: Optional[str] = None
        self.token_expiry: float = 0.0
        self.last_error: str = ""

    def update_config(self, host: Optional[str] = None, port: Optional[int] = None, username: Optional[str] = None, password: Optional[str] = None):
        """Update client connection credentials and clear cached token."""
        changed = False
        if host is not None and host != self.host:
            self.host = host
            changed = True
        if port is not None and port != self.port:
            self.port = port
            changed = True
        if username is not None and username != self.username:
            self.username = username
            changed = True
        if password is not None and password != self.password:
            self.password = password
            changed = True
        if changed:
            self.base_url = f"http://{self.host}:{self.port}"
            self.token = None
            self.token_expiry = 0.0
            self.last_error = ""
            logger.info("飞牛音乐客户端配置已更新: %s:%s, 用户名: %s", self.host, self.port, self.username)

    def _sign_authx(self, method: str, path: str, data: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> str:
        nonce = str(random.randint(100000, 999999))
        ts = str(int(time.time() * 1000))
        if method.upper() == "GET":
            if params:
                sorted_keys = sorted(params.keys())
                q_parts = [f"{k}={params[k]}" for k in sorted_keys if params[k] is not None]
                body_md5 = hashlib.md5("&".join(q_parts).encode("utf-8")).hexdigest()
            else:
                body_md5 = hashlib.md5(b"").hexdigest()
        else:
            body_str = json.dumps(data, separators=(",", ":")) if data is not None else ""
            body_md5 = hashlib.md5(body_str.encode("utf-8")).hexdigest()

        sign_str = "_".join([FNOS_PREFIX, path, nonce, ts, body_md5, FNOS_API_KEY])
        sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()
        return f"nonce={nonce}&timestamp={ts}&sign={sign}"

    async def login(self) -> bool:
        """Authenticate with fnOS Music service using passwordLogin."""
        logger.info("正在尝试登录飞牛音乐: %s/music/api/v1/user/password-login (用户: %s)", self.base_url, self.username)
        pwd_hash = hashlib.sha256(self.password.encode("utf-8")).hexdigest()
        payload = {
            "username": self.username,
            "password": pwd_hash,
            "deviceId": DEFAULT_DEVICE_ID
        }
        path = "/music/api/v1/user/password-login"
        authx = self._sign_authx("POST", path, data=payload)
        url = f"{self.base_url}{path}"

        headers = {
            "Content-Type": "application/json",
            "authx": authx,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    user_token = (data.get("data") or {}).get("userToken")
                    if data.get("code") == 0 and user_token:
                        self.token = user_token
                        # Token cached for 24 hours (proactive refresh rather than stale 7-day token)
                        self.token_expiry = time.time() + 86400
                        self.last_error = ""
                        logger.info("✅ 飞牛音乐登录成功！Token 已获取")
                        return True
                    else:
                        msg = data.get("msg") or f"错误代码 {data.get('code')}"
                        self.last_error = f"飞牛音乐验证失败: {msg}"
                        logger.error("❌ 飞牛音乐登录失败: %s", msg)
                elif resp.status_code == 504:
                    self.last_error = "飞牛 Nginx 网关超时 (504)，飞牛音乐服务内部无响应，请在系统【应用中心】重启飞牛音乐"
                    logger.error("⚠️ 飞牛音乐网关超时 504 Gateway Timeout: 音乐服务内部进程卡死，需在飞牛应用中心重启")
                else:
                    self.last_error = f"飞牛服务返回 HTTP {resp.status_code}: {resp.text[:80]}"
                    logger.error("❌ 飞牛音乐登录返回 HTTP %s: %s", resp.status_code, resp.text[:120])
            except httpx.TimeoutException:
                self.last_error = f"连接飞牛音乐服务 ({self.host}:{self.port}) 超时，请检查 IP/端口是否畅通"
                logger.error("⚠️ 连接飞牛音乐服务 (%s:%s) 超时 (10s)", self.host, self.port)
            except Exception as e:
                self.last_error = f"连接异常: {e}"
                logger.error("❌ 连接飞牛音乐服务异常: %r", e)
        return False

    def invalidate_token(self):
        """Force invalidate cached token to trigger re-authentication."""
        self.token = None
        self.token_expiry = 0.0

    async def force_relogin(self) -> bool:
        """Force clear token and perform fresh login."""
        self.invalidate_token()
        return await self.login()

    async def ensure_token(self) -> bool:
        if not self.token or time.time() >= self.token_expiry:
            logger.info("当前凭据已过期或未获取，正在自动重新鉴权...")
            return await self.login()
        return True

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 12.0) -> Optional[Dict[str, Any]]:
        """Perform authenticated GET request with auto-recovery on token expiration or 100001 session errors."""
        if not await self.ensure_token():
            return None

        for attempt in range(2):
            token = self.token
            authx = self._sign_authx("GET", path, params=params)
            url = f"{self.base_url}{path}"
            headers = {
                "authx": authx,
                "Cookie": f"music-token={token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            try:
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 401 and attempt == 0:
                        logger.warning("飞牛音乐返回 HTTP 401 Unauthorized，尝试自动重新登录...")
                        if await self.force_relogin():
                            continue
                        return None

                    if resp.status_code == 200:
                        d = resp.json()
                        code = d.get("code")
                        if code == 0:
                            return d
                        # 100001: generic/session invalid, 100002/100004: unauthorized
                        if code in (100001, 100002, 100004) and attempt == 0:
                            logger.warning("⚠️ 飞牛音乐接口返回错误码 %s (%s)，尝试重新登录恢复会话...", code, d.get("msg"))
                            if await self.force_relogin():
                                continue
                        logger.error("飞牛音乐接口返回非零业务代码: %s, 消息: %s", code, d.get("msg"))
                        return None
                    else:
                        logger.error("飞牛音乐接口返回 HTTP %s: %s", resp.status_code, resp.text[:100])
                        return None
            except Exception as e:
                logger.error("请求飞牛音乐接口 [%s] 异常: %s", path, e)
                if attempt == 0:
                    await self.force_relogin()
                    continue
                return None
        return None

    async def get_tracks(self, page: int = 1, page_size: int = 50) -> List[Dict[str, Any]]:
        """Fetch list of tracks from fnOS Music."""
        path = "/music/api/v1/track/list"
        params = {"page": str(page), "pageSize": str(page_size)}
        logger.info("正在拉取本地曲库歌曲列表 (页码: %s, 数量: %s)...", page, page_size)
        d = await self._get_json(path, params=params)
        if d:
            track_list = (d.get("data") or {}).get("list", [])
            logger.info("✅ 成功拉取本地曲库，获取到 %d 首歌曲", len(track_list))
            return track_list
        return []

    async def search_tracks(self, query: str, page: int = 1, page_size: int = 30) -> List[Dict[str, Any]]:
        """Search tracks in fnOS Music library."""
        path = "/music/api/v1/search/track"
        params = {"q": query, "page": str(page), "pageSize": str(page_size)}
        logger.info("正在搜索飞牛音乐: 关键词='%s'", query)
        d = await self._get_json(path, params=params)
        if d:
            res_list = (d.get("data") or {}).get("list", [])
            logger.info("✅ 搜索完成，找到 %d 首匹配歌曲", len(res_list))
            return res_list
        return []

    async def get_playlists(self) -> List[Dict[str, Any]]:
        """Retrieve user playlists from fnOS Music."""
        path = "/music/api/v1/playlist/list"
        logger.info("正在请求飞牛音乐歌单列表...")
        d = await self._get_json(path)
        if d:
            pl_list = (d.get("data") or {}).get("list", [])
            logger.info("✅ 成功获取飞牛歌单列表，共 %d 个歌单", len(pl_list))
            return pl_list
        return []

    async def get_playlist_tracks(self, playlist_id: str, page: int = 1, size: int = 200) -> List[Dict[str, Any]]:
        """Retrieve tracks for a given playlist from fnOS Music."""
        path = "/music/api/v1/track/playlist-detail/list"
        params = {
            "page": str(page),
            "playlistGUID": playlist_id,
            "size": str(size)
        }
        logger.info("正在获取歌单 [%s] 的曲目 (size: %s)...", playlist_id, size)
        d = await self._get_json(path, params=params, timeout=15.0)
        if d:
            tracks = (d.get("data") or {}).get("list", [])
            logger.info("✅ 成功获取歌单 [%s] 曲目，共 %d 首歌曲", playlist_id, len(tracks))
            return tracks
        return []

    async def get_all_tracks(self, max_tracks: int = 3000) -> List[Dict[str, Any]]:
        """Fetch all tracks from fnOS Music library with automatic pagination."""
        all_tracks = []
        page = 1
        page_size = 100
        while len(all_tracks) < max_tracks:
            batch = await self.get_tracks(page=page, page_size=page_size)
            if not batch:
                break
            all_tracks.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        logger.info("✅ 本地曲库累计拉取完成，共 %d 首歌曲", len(all_tracks))
        return all_tracks

    async def get_all_playlist_tracks(self, playlist_id: str, max_tracks: int = 3000) -> List[Dict[str, Any]]:
        """Retrieve tracks for a playlist with automatic multi-page loading up to max_tracks."""
        all_tracks = []
        page = 1
        page_size = 200
        while len(all_tracks) < max_tracks:
            batch = await self.get_playlist_tracks(playlist_id, page=page, size=page_size)
            if not batch:
                break
            all_tracks.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        logger.info("✅ 歌单 [%s] 累计拉取完成，共 %d 首歌曲", playlist_id, len(all_tracks))
        return all_tracks

    def get_stream_url(self, guid: str) -> str:
        """Returns direct proxy streaming URL in nas-stats."""
        return f"/api/music/fnos/stream?guid={guid}"

    def get_cover_url(self, cover_id: str) -> str:
        """Returns direct proxy cover URL in nas-stats."""
        if not cover_id:
            return ""
        return f"/api/music/fnos/cover?coverId={cover_id}"


fnmusic_client = FnMusicClient()
