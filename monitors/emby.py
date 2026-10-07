"""Emby media server monitor module."""

import httpx
from .base import BaseMonitor


class EmbyMonitor(BaseMonitor):
    display_name = "Emby"

    async def fetch(self) -> dict:
        url = self.config.get("url")
        if not url:
            return {"enabled": True, "status": "error", "error": "未配置 URL"}

        api_key = self.config.get("api_key", "")
        headers = {}
        params = {}
        if api_key:
            params["api_key"] = api_key
            headers["X-Emby-Token"] = api_key

        client = self.client or httpx.AsyncClient(timeout=8)

        try:
            # 1. System info
            info_resp = await client.get(self._url("/emby/System/Info"), headers=headers, params=params, timeout=5)
            if info_resp.status_code == 401 or info_resp.status_code == 403:
                return {"enabled": True, "status": "auth_failed", "error": "API Key 无效"}

            server_name = ""
            version = ""
            if info_resp.status_code == 200:
                info_data = info_resp.json()
                server_name = info_data.get("ServerName", "")
                version = info_data.get("Version", "")

            # 2. Active sessions
            sessions_count = 0
            playing_items = []
            try:
                sess_resp = await client.get(self._url("/emby/Sessions"), headers=headers, params=params, timeout=5)
                if sess_resp.status_code == 200:
                    sessions = sess_resp.json()
                    for s in sessions:
                        now_playing = s.get("NowPlayingItem")
                        if now_playing:
                            sessions_count += 1
                            playing_items.append({
                                "user": s.get("UserName", ""),
                                "title": now_playing.get("Name", ""),
                                "type": now_playing.get("Type", ""),
                                "client": s.get("Client", ""),
                                "device": s.get("DeviceName", ""),
                            })
            except Exception:
                pass

            # 3. Item counts / library summary
            movies = 0
            series = 0
            episodes = 0
            try:
                counts_resp = await client.get(self._url("/emby/Items/Counts"), headers=headers, params=params, timeout=5)
                if counts_resp.status_code == 200:
                    counts = counts_resp.json()
                    movies = counts.get("MovieCount", 0)
                    series = counts.get("SeriesCount", 0)
                    episodes = counts.get("EpisodeCount", 0)
            except Exception:
                pass

            return {
                "enabled": True,
                "status": "ok",
                "server_name": server_name,
                "version": version,
                "active_streams": sessions_count,
                "playing_items": playing_items[:5],
                "movie_count": movies,
                "series_count": series,
                "episode_count": episodes,
            }
        except Exception as e:
            return {"enabled": True, "status": "error", "error": str(e)}
