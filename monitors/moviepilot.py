"""MoviePilot monitor module."""

import httpx
from .base import BaseMonitor


class MoviePilotMonitor(BaseMonitor):
    display_name = "MoviePilot"

    async def fetch(self) -> dict:
        svc = self.config
        base = (svc.get("url") or "").rstrip("/")
        if not base:
            return {"enabled": True, "status": "error", "error": "未配置 URL"}

        api_key = svc.get("api_key", "")
        if not api_key:
            return {"enabled": True, "status": "error", "error": "需要 API Key"}

        client = self.client or httpx.AsyncClient(timeout=10)
        params = {"apikey": api_key}
        result = {
            "enabled": True,
            "status": "ok",
            "show_recent": svc.get("show_recent", True),
            "show_sites": svc.get("show_sites", True),
            "show_downloads": svc.get("show_downloads", True),
        }

        try:
            resp = await client.get(f"{base}/", params=params, timeout=5)
            if resp.status_code != 200:
                return {"enabled": True, "status": "error", "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"enabled": True, "status": "error", "error": str(e)}

        try:
            stat_resp = await client.get(f"{base}/api/v1/dashboard/statistic", params=params, timeout=8)
            if stat_resp.status_code == 200:
                stat = stat_resp.json().get("data", {})
                result["library"] = {
                    "movie_count": stat.get("movie_count", 0),
                    "tv_count": stat.get("tv_count", 0),
                    "movie_month": stat.get("movie_count_month", 0),
                    "tv_month": stat.get("tv_count_month", 0),
                    "episode_month": stat.get("episode_count_month", 0),
                }
        except Exception:
            pass

        try:
            dl_resp = await client.get(f"{base}/api/v1/dashboard/downloader", params=params, timeout=8)
            if dl_resp.status_code == 200:
                dl = dl_resp.json().get("data", {})
                result["downloader"] = {
                    "dl_speed": dl.get("download_speed", 0),
                    "ul_speed": dl.get("upload_speed", 0),
                    "dl_size": dl.get("download_size", 0),
                    "ul_size": dl.get("upload_size", 0),
                    "free_space": dl.get("free_space", 0),
                }
        except Exception:
            pass

        try:
            site_resp = await client.get(f"{base}/api/v1/site/statistic", params=params, timeout=10)
            if site_resp.status_code == 200:
                sites = site_resp.json().get("data", [])
                ok_count = sum(1 for s in sites if s.get("lst_state") == 0)
                fail_count = sum(1 for s in sites if s.get("lst_state") != 0)
                result["sites"] = {
                    "total": len(sites),
                    "ok": ok_count,
                    "fail": fail_count,
                }
        except Exception:
            pass

        try:
            hist_resp = await client.get(f"{base}/api/v1/history/transfer", params={**params, "page": 1, "count": 5}, timeout=8)
            if hist_resp.status_code == 200:
                hist_data = hist_resp.json().get("data", {})
                items = hist_data.get("list", []) if isinstance(hist_data, dict) else hist_data
                recent = []
                for item in items[:5]:
                    recent.append({
                        "title": item.get("title", ""),
                        "year": item.get("year", ""),
                        "type": item.get("type", ""),
                        "image": item.get("image", ""),
                        "date": item.get("date", ""),
                    })
                result["recent"] = recent
        except Exception:
            pass

        return result
