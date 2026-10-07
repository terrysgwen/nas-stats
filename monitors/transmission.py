"""Transmission monitor module."""

import httpx
from .base import BaseMonitor


class TransmissionMonitor(BaseMonitor):
    display_name = "Transmission"

    def __init__(self, monitor_id: str, config: dict):
        super().__init__(monitor_id, config)
        self._session_id = ""

    async def fetch(self) -> dict:
        url = self.config.get("url")
        if not url:
            return {"enabled": True, "status": "error", "error": "未配置 URL"}

        auth = None
        if self.config.get("username"):
            auth = (self.config.get("username", ""), self.config.get("password", ""))

        headers = {}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id

        client = self.client or httpx.AsyncClient(timeout=10)

        for attempt in range(2):
            try:
                body = {"method": "session-stats", "arguments": {}}
                resp = await client.post(
                    self._url("/transmission/rpc"),
                    json=body,
                    auth=auth,
                    headers=headers,
                )

                if resp.status_code == 409:
                    self._session_id = resp.headers.get("X-Transmission-Session-Id", "")
                    headers["X-Transmission-Session-Id"] = self._session_id
                    continue

                if resp.status_code != 200:
                    return {"enabled": True, "status": "error", "error": f"HTTP {resp.status_code}"}

                stats = resp.json().get("arguments", {})

                body2 = {
                    "method": "torrent-get",
                    "arguments": {
                        "fields": [
                            "status", "rateDownload", "rateUpload", "percentDone",
                            "error", "errorString", "name", "totalSize", "eta"
                        ],
                    },
                }
                resp2 = await client.post(
                    self._url("/transmission/rpc"),
                    json=body2,
                    auth=auth,
                    headers=headers,
                )
                torrents = resp2.json().get("arguments", {}).get("torrents", [])

                downloading = sum(1 for t in torrents if t.get("status") == 4)
                seeding = sum(1 for t in torrents if t.get("status") == 8)
                paused = sum(1 for t in torrents if t.get("status") in (0, 1, 2))
                errored = sum(1 for t in torrents if t.get("error", 0) > 0)

                dl_torrents = []
                for t in torrents:
                    if t.get("status") == 4:
                        dl_torrents.append({
                            "name": t.get("name", ""),
                            "size": t.get("totalSize", 0),
                            "progress": t.get("percentDone", 0),
                            "dl_speed": t.get("rateDownload", 0),
                            "eta": t.get("eta", 0),
                        })

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
                    "dl_torrents": dl_torrents[:10],
                }
            except Exception as e:
                if attempt == 0:
                    continue
                return {"enabled": True, "status": "error", "error": str(e)}

        return {"enabled": True, "status": "error", "error": "CSRF 会话获取失败"}
