"""iStoreOS / OpenWrt monitor module.

Supports multiple OpenWrt / iStoreOS API interfaces:
1. Standard ubus RPC endpoint: /ubus (session login via jsonrpc)
2. LuCI RPC endpoint: /cgi-bin/luci/rpc/auth & /cgi-bin/luci/rpc/sys
3. Web interface direct probe with basic HTTP status fallback
"""

import httpx
from .base import BaseMonitor


class IStoreOSMonitor(BaseMonitor):
    display_name = "iStoreOS"

    def __init__(self, monitor_id: str, config: dict):
        super().__init__(monitor_id, config)
        self._ubus_session = ""
        self._luci_token = ""

    def _format_url(self) -> str:
        raw_url = (self.config.get("url") or "").strip().rstrip("/")
        if not raw_url:
            return ""
        if not raw_url.startswith(("http://", "https://")):
            raw_url = "http://" + raw_url
        return raw_url

    async def _try_ubus(self, client: httpx.AsyncClient, base: str) -> dict | None:
        """Attempt to fetch system info via OpenWrt /ubus jsonrpc."""
        username = (self.config.get("username") or "root").strip()
        password = (self.config.get("password") or "").strip()

        # Login to ubus if no session
        if not self._ubus_session:
            try:
                login_payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "call",
                    "params": [
                        "00000000000000000000000000000000",
                        "session",
                        "login",
                        {"username": username, "password": password}
                    ]
                }
                resp = await client.post(f"{base}/ubus", json=login_payload, timeout=4)
                if resp.status_code == 200:
                    data = resp.json().get("result", [])
                    if len(data) >= 2 and isinstance(data[1], dict):
                        self._ubus_session = data[1].get("ubus_rpc_session", "")
            except Exception:
                pass

        if self._ubus_session:
            try:
                info_payload = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "call",
                    "params": [
                        self._ubus_session,
                        "system",
                        "info",
                        {}
                    ]
                }
                resp = await client.post(f"{base}/ubus", json=info_payload, timeout=4)
                if resp.status_code == 200:
                    res = resp.json().get("result", [])
                    if len(res) >= 2 and isinstance(res[1], dict):
                        sysinfo = res[1]
                        mem = sysinfo.get("memory", {})
                        total = mem.get("total", 0)
                        free = mem.get("free", 0) + mem.get("buffered", 0) + mem.get("cached", 0)
                        used = max(0, total - free)
                        pct = round(used / total * 100, 1) if total else 0
                        load = sysinfo.get("load", [0, 0, 0])
                        load_avg = round(load[0] / 65536.0, 2) if load and isinstance(load[0], int) and load[0] > 100 else (load[0] if load else 0)
                        return {
                            "enabled": True,
                            "status": "ok",
                            "mem_total": total,
                            "mem_used": used,
                            "mem_pct": pct,
                            "load_avg": load_avg,
                            "uptime": sysinfo.get("uptime", 0),
                        }
            except Exception:
                self._ubus_session = ""
        return None

    async def _try_luci_rpc(self, client: httpx.AsyncClient, base: str) -> dict | None:
        """Attempt to fetch via /cgi-bin/luci/rpc."""
        username = (self.config.get("username") or "root").strip()
        password = (self.config.get("password") or "").strip()

        if not self._luci_token:
            try:
                resp = await client.post(
                    f"{base}/cgi-bin/luci/rpc/auth",
                    json={"id": 1, "method": "login", "params": [username, password]},
                    timeout=4
                )
                if resp.status_code == 200:
                    token = resp.json().get("result")
                    if token:
                        self._luci_token = token
            except Exception:
                pass

        if self._luci_token:
            try:
                resp = await client.post(
                    f"{base}/cgi-bin/luci/rpc/sys?auth={self._luci_token}",
                    json={"id": 1, "method": "sysinfo", "params": []},
                    timeout=4
                )
                if resp.status_code == 200:
                    data = resp.json().get("result", {})
                    if data:
                        total = data.get("totalram", 0)
                        free = data.get("freeram", 0) + data.get("bufferram", 0)
                        used = max(0, total - free)
                        pct = round(used / total * 100, 1) if total else 0
                        loads = data.get("loads", [0, 0, 0])
                        load_avg = round(loads[0] / 65536.0, 2) if loads and isinstance(loads[0], int) and loads[0] > 100 else (loads[0] if loads else 0)
                        return {
                            "enabled": True,
                            "status": "ok",
                            "mem_total": total,
                            "mem_used": used,
                            "mem_pct": pct,
                            "load_avg": load_avg,
                            "uptime": data.get("uptime", 0),
                        }
            except Exception:
                self._luci_token = ""
        return None

    async def fetch(self) -> dict:
        base = self._format_url()
        if not base:
            return {"enabled": True, "status": "error", "error": "未填写 iStoreOS 地址 (如 http://192.168.1.1)"}

        client = self.client or httpx.AsyncClient(timeout=6, verify=False)

        # 1. Try ubus first (OpenWrt default standard)
        res = await self._try_ubus(client, base)
        if res:
            return res

        # 2. Try LuCI RPC
        res = await self._try_luci_rpc(client, base)
        if res:
            return res

        # 3. Fallback: Check if Web UI is reachable
        try:
            r = await client.get(f"{base}/cgi-bin/luci", timeout=4)
            if r.status_code in (200, 301, 302, 403):
                return {
                    "enabled": True,
                    "status": "ok",
                    "online": True,
                    "mem_pct": 0,
                    "message": "iStoreOS Web 在线 (未开启 ubus/rpc)"
                }
        except Exception as e:
            return {"enabled": True, "status": "error", "error": f"无法连通: {str(e)}"}

        return {"enabled": True, "status": "error", "error": "无法连接到 iStoreOS (请检查 IP 或账号密码)"}
