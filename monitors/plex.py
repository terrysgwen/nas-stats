"""Plex media server monitor module."""

import httpx
from .base import BaseMonitor


class PlexMonitor(BaseMonitor):
    display_name = "Plex"

    async def fetch(self) -> dict:
        url = self.config.get("url")
        if not url:
            return {"enabled": True, "status": "error", "error": "未配置 URL"}

        token = self.config.get("token", "")
        headers = {
            "Accept": "application/json",
        }
        if token:
            headers["X-Plex-Token"] = token

        client = self.client or httpx.AsyncClient(timeout=8)

        try:
            # 1. Server identity
            id_resp = await client.get(self._url("/identity"), headers=headers, timeout=5)
            if id_resp.status_code == 401 or id_resp.status_code == 403:
                return {"enabled": True, "status": "auth_failed", "error": "X-Plex-Token 缺失或无效"}

            server_name = ""
            version = ""
            try:
                root_resp = await client.get(self._url("/"), headers=headers, timeout=5)
                if root_resp.status_code == 200:
                    data = root_resp.json().get("MediaContainer", {})
                    server_name = data.get("friendlyName", "")
                    version = data.get("version", "")
            except Exception:
                pass

            # 2. Active sessions
            active_streams = 0
            playing_items = []
            try:
                sess_resp = await client.get(self._url("/status/sessions"), headers=headers, timeout=5)
                if sess_resp.status_code == 200:
                    container = sess_resp.json().get("MediaContainer", {})
                    active_streams = container.get("size", 0)
                    metadata = container.get("Metadata", [])
                    for item in metadata:
                        user = item.get("User", {}).get("title", "")
                        player = item.get("Player", {}).get("title", "")
                        playing_items.append({
                            "user": user,
                            "title": item.get("title", ""),
                            "grandparentTitle": item.get("grandparentTitle", ""),
                            "type": item.get("type", ""),
                            "player": player,
                        })
            except Exception:
                pass

            # 3. Library sections count
            sections_count = 0
            try:
                sec_resp = await client.get(self._url("/library/sections"), headers=headers, timeout=5)
                if sec_resp.status_code == 200:
                    sections_count = sec_resp.json().get("MediaContainer", {}).get("size", 0)
            except Exception:
                pass

            return {
                "enabled": True,
                "status": "ok",
                "server_name": server_name,
                "version": version,
                "active_streams": active_streams,
                "playing_items": playing_items[:5],
                "sections_count": sections_count,
            }
        except Exception as e:
            return {"enabled": True, "status": "error", "error": str(e)}
