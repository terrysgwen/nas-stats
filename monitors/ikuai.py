"""iKuai router monitor module."""

import base64
import hashlib
import json
import httpx
from .base import BaseMonitor


class IKuaiMonitor(BaseMonitor):
    display_name = "爱快路由器"

    def __init__(self, monitor_id: str, config: dict):
        super().__init__(monitor_id, config)
        self._ik_client: httpx.AsyncClient | None = None
        self._logged_in = False

    def _get_client(self) -> httpx.AsyncClient:
        if self._ik_client is None or self._ik_client.is_closed:
            self._ik_client = httpx.AsyncClient(timeout=10, follow_redirects=True)
        return self._ik_client

    async def _login(self) -> bool:
        client = self._get_client()
        svc = self.config
        try:
            passwd_md5 = hashlib.md5((svc.get("password") or "").encode()).hexdigest()
            pass_encoded = base64.b64encode(f"salt_11{passwd_md5}".encode()).decode()
            resp = await client.post(
                self._url("/Action/login"),
                json={
                    "username": svc.get("username", "admin"),
                    "passwd": passwd_md5,
                    "pass": pass_encoded,
                    "remember_password": "",
                },
            )
            data = resp.json()
            self._logged_in = (
                data.get("Data", {}).get("login") == 1 or resp.cookies.get("sess_key") is not None
            )
            return self._logged_in
        except Exception:
            self._logged_in = False
            return False

    async def _call(self, func_name: str, action: str = "", param: dict = None) -> dict:
        client = self._get_client()
        payload = {"func_name": func_name, "action": action}
        if param:
            payload["param"] = param
        try:
            resp = await client.post(self._url("/Action/call"), json=payload)
            return resp.json()
        except Exception:
            return {}

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

                sysstat = await self._call("sysstat", "show", {"TYPE": "verinfo,cpu,memory,stream,cputemp"})
                wan_resp = await self._call("wan", "show", {})

                if (sysstat and sysstat.get("code") == 1008) or (wan_resp and wan_resp.get("code") == 1008):
                    self._logged_in = False
                    client.cookies.clear()
                    continue

                if not sysstat and not wan_resp:
                    self._logged_in = False
                    client.cookies.clear()
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
                    self._logged_in = False
                    client.cookies.clear()
                    continue
                return {"enabled": True, "status": "error", "error": str(e)}

        return {"enabled": True, "status": "auth_failed", "error": "认证失败"}
