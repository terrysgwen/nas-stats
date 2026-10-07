"""qBittorrent monitor module."""

import httpx
from .base import BaseMonitor


class QBittorrentMonitor(BaseMonitor):
    display_name = "qBittorrent"

    def __init__(self, monitor_id: str, config: dict):
        super().__init__(monitor_id, config)
        self._qb_client: httpx.AsyncClient | None = None
        self._logged_in = False

    def _get_client(self) -> httpx.AsyncClient:
        if self._qb_client is None or self._qb_client.is_closed:
            self._qb_client = httpx.AsyncClient(timeout=10, follow_redirects=True)
        return self._qb_client

    async def _login(self) -> bool:
        client = self._get_client()
        url = self._url("/api/v2/auth/login")
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        try:
            resp = await client.post(url, data={"username": username, "password": password})
            self._logged_in = (resp.text.strip() == "Ok.")
            return self._logged_in
        except Exception:
            self._logged_in = False
            return False

    async def fetch(self) -> dict:
        url = self.config.get("url")
        if not url:
            return {"enabled": True, "status": "error", "error": "未配置 URL"}

        client = self._get_client()

        for attempt in range(2):
            try:
                if not self._logged_in:
                    if not await self._login():
                        return {"enabled": True, "status": "auth_failed", "error": "认证失败"}

                transfer_resp = await client.get(self._url("/api/v2/transfer/info"))
                torrents_resp = await client.get(self._url("/api/v2/torrents/info"))

                if transfer_resp.status_code == 403 or torrents_resp.status_code == 403:
                    self._logged_in = False
                    client.cookies.clear()
                    continue

                if transfer_resp.status_code != 200 or torrents_resp.status_code != 200:
                    self._logged_in = False
                    client.cookies.clear()
                    continue

                transfer = transfer_resp.json()
                torrents = torrents_resp.json()

                downloading = sum(1 for t in torrents if t.get("state") in ("downloading", "stalledDL", "metaDL", "forcedMetaDL"))
                seeding = sum(1 for t in torrents if t.get("state") in ("uploading", "stalledUP", "forcedUP"))
                paused = sum(1 for t in torrents if t.get("state") in ("pausedDL", "pausedUP"))
                errored = sum(1 for t in torrents if t.get("state") in ("error", "missingFiles"))

                dl_torrents = []
                for t in torrents:
                    if t.get("state") in ("downloading", "stalledDL", "metaDL", "forcedMetaDL"):
                        dl_torrents.append({
                            "name": t.get("name", ""),
                            "size": t.get("total_size", 0),
                            "progress": t.get("progress", 0),
                            "dl_speed": t.get("dlspeed", 0),
                            "eta": t.get("eta", 0),
                        })

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
                    "dl_torrents": dl_torrents[:10],
                }
            except Exception as e:
                if attempt == 0:
                    self._logged_in = False
                    client.cookies.clear()
                    continue
                return {"enabled": True, "status": "error", "error": str(e)}

        return {"enabled": True, "status": "auth_failed", "error": "认证重试失败"}
