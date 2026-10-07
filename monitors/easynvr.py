"""EasyNVR video monitor module.

Compatible with modern EasyNVR Cloud / Go-based backend (e.g. Speed 12Q / ONVIF v5+):
- Authentication: POST /login with sha256(password), Bearer token
- Channels: GET /channels (returns {"items": [...]})
- Snapshot: GET /channels/{id}/snapshot (returns {"img": "base64..."})
- Legacy EasyDarwin fallback: /api/v1/getchannels, /api/v1/login
"""

import hashlib
from urllib.parse import urljoin
import httpx
from .base import BaseMonitor


class EasyNVRMonitor(BaseMonitor):
    display_name = "EasyNVR"

    def __init__(self, monitor_id: str, config: dict):
        super().__init__(monitor_id, config)
        self._token = ""
        self._is_modern_api = True

    def _format_url(self) -> str:
        raw_url = (self.config.get("url") or "").strip().rstrip("/")
        if not raw_url:
            return ""
        if not raw_url.startswith(("http://", "https://")):
            raw_url = "http://" + raw_url
        return raw_url

    async def _login(self, client: httpx.AsyncClient, base: str) -> bool:
        username = (self.config.get("username") or "admin").strip()
        raw_password = (self.config.get("password") or "").strip()
        sha256_pass = hashlib.sha256(raw_password.encode()).hexdigest()

        # 1. Try modern EasyNVR Cloud login: POST /login with SHA256 password
        try:
            resp = await client.post(
                f"{base}/login",
                json={"username": username, "password": sha256_pass},
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token") or data.get("data", {}).get("token")
                if token:
                    self._token = token
                    self._is_modern_api = True
                    return True
        except Exception:
            pass

        # 2. Try POST /login with raw password
        try:
            resp = await client.post(
                f"{base}/login",
                json={"username": username, "password": raw_password},
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token") or data.get("data", {}).get("token")
                if token:
                    self._token = token
                    self._is_modern_api = True
                    return True
        except Exception:
            pass

        # 3. Fallback to legacy EasyDarwin API: GET /api/v1/login
        try:
            resp = await client.get(
                f"{base}/api/v1/login",
                params={"username": username, "password": raw_password},
                timeout=5
            )
            if resp.status_code == 200:
                ed = resp.json().get("EasyDarwin", {})
                token = ed.get("Body", {}).get("Token", "")
                if token:
                    self._token = token
                    self._is_modern_api = False
                    return True
        except Exception:
            pass

        return False

    async def fetch(self) -> dict:
        base = self._format_url()
        if not base:
            return {"enabled": True, "status": "error", "error": "未填写 EasyNVR 地址"}

        client = self.client or httpx.AsyncClient(timeout=8, verify=False)

        for attempt in range(2):
            if not self._token:
                if not await self._login(client, base):
                    return {"enabled": True, "status": "auth_failed", "error": "EasyNVR 登录失败 (账号或密码错误)"}

            headers = {
                "Authorization": f"Bearer {self._token}",
                "token": self._token,
            }

            try:
                channels = []
                online_count = 0
                total_count = 0

                # ── Path A: Modern EasyNVR Cloud (GET /channels) ──
                if self._is_modern_api:
                    r_ch = await client.get(f"{base}/channels", headers=headers, timeout=6)
                    if r_ch.status_code in (401, 403):
                        self._token = ""
                        if attempt == 0:
                            continue
                        return {"enabled": True, "status": "auth_failed", "error": "EasyNVR Token 失效"}

                    if r_ch.status_code == 200:
                        items = r_ch.json().get("items", [])
                        total_count = len(items)
                        for item in items:
                            ch_id = item.get("id", "")
                            is_online = bool(item.get("status") or item.get("enabled"))
                            if is_online:
                                online_count += 1

                            name = item.get("custom_name") or item.get("name") or ch_id

                            # Fetch snapshot base64 image
                            snap_data = ""
                            try:
                                r_snap = await client.get(
                                    f"{base}/channels/{ch_id}/snapshot",
                                    headers=headers,
                                    timeout=3
                                )
                                if r_snap.status_code == 200:
                                    img_b64 = r_snap.json().get("img", "")
                                    if img_b64:
                                        if not img_b64.startswith("data:"):
                                            snap_data = f"data:image/jpeg;base64,{img_b64}"
                                        else:
                                            snap_data = img_b64
                            except Exception:
                                pass

                            channels.append({
                                "channel": ch_id,
                                "name": name,
                                "online": is_online,
                                "snap_url": snap_data,
                                "flv": f"{base}/cloud/#/preview",
                            })

                        return {
                            "enabled": True,
                            "status": "ok",
                            "total_channels": total_count,
                            "online_channels": online_count,
                            "channels": channels,
                        }

                # ── Path B: Legacy EasyDarwin (/api/v1/getchannels) ──
                r_ch = await client.get(f"{base}/api/v1/getchannels", params={"token": self._token}, timeout=6)
                if r_ch.status_code == 200:
                    resp_json = r_ch.json()
                    body = resp_json.get("EasyDarwin", {}).get("Body", {})
                    channel_list = body.get("Channels") or body.get("ChannelList") or []
                    total_count = len(channel_list)

                    for ch in channel_list:
                        is_online = bool(ch.get("Online") or ch.get("Status") == "ON" or ch.get("ChannelOnline"))
                        if is_online:
                            online_count += 1
                        snap = ch.get("SnapURL") or ""
                        if snap and not snap.startswith(("http://", "https://")):
                            snap = urljoin(base, snap)

                        channels.append({
                            "channel": ch.get("Channel") or ch.get("ChannelID") or ch.get("ID"),
                            "name": ch.get("Name") or f"通道 {ch.get('Channel')}",
                            "online": is_online,
                            "snap_url": snap,
                            "flv": ch.get("FLV") or f"{base}/cloud/#/preview",
                        })

                    return {
                        "enabled": True,
                        "status": "ok",
                        "total_channels": total_count,
                        "online_channels": online_count,
                        "channels": channels,
                    }

                return {"enabled": True, "status": "error", "error": f"无法获取通道列表 (HTTP {r_ch.status_code})"}

            except Exception as e:
                if attempt == 0:
                    self._token = ""
                    continue
                return {"enabled": True, "status": "error", "error": f"EasyNVR 请求异常: {str(e)}"}

        return {"enabled": True, "status": "error", "error": "EasyNVR 连接超时"}
