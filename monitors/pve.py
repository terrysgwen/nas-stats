"""Proxmox VE monitor module.

Supports two authentication modes:
1. User password (ticket-based login) - Simplest! Just url, username, password.
2. API Token (token_id + token_secret).

Also supports automatic node detection if node is left blank or inaccurate.
"""

import httpx
from .base import BaseMonitor


class PVEMonitor(BaseMonitor):
    display_name = "Proxmox VE"

    def __init__(self, monitor_id: str, config: dict):
        super().__init__(monitor_id, config)
        self._pve_client: httpx.AsyncClient | None = None
        self._ticket = ""
        self._csrf = ""

    def _get_client(self) -> httpx.AsyncClient:
        if self._pve_client is None or self._pve_client.is_closed:
            self._pve_client = httpx.AsyncClient(timeout=10, verify=False)
        return self._pve_client

    async def _login_with_password(self, client: httpx.AsyncClient, base: str) -> bool:
        username = (self.config.get("username") or "root").strip()
        if "@" not in username:
            username = f"{username}@pam"
        password = self.config.get("password") or ""
        if not password:
            return False

        try:
            resp = await client.post(
                f"{base}/api2/json/access/ticket",
                data={"username": username, "password": password},
                timeout=6
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                self._ticket = data.get("ticket", "")
                self._csrf = data.get("CSRFPreventionToken", "")
                return bool(self._ticket)
        except Exception:
            pass
        return False

    async def _get_auth_headers_and_cookies(self, client: httpx.AsyncClient, base: str) -> tuple[dict, dict, str]:
        """Returns (headers, cookies, error_msg)."""
        token_id = (self.config.get("token_id") or "").strip()
        token_secret = (self.config.get("token_secret") or "").strip()

        # 1. Prefer API Token if both token_id and token_secret are filled
        if token_id and token_secret:
            username = (self.config.get("username") or "root").strip()
            if "@" not in username:
                username = f"{username}@pam"
            headers = {
                "Authorization": f"PVEAPIToken={username}!{token_id}={token_secret}"
            }
            return headers, {}, ""

        # 2. Otherwise fallback to Username + Password
        password = (self.config.get("password") or "").strip()
        if password:
            if not self._ticket:
                if not await self._login_with_password(client, base):
                    return {}, {}, "账号或密码错误"
            headers = {"CSRFPreventionToken": self._csrf}
            cookies = {"PVEAuthCookie": self._ticket}
            return headers, cookies, ""

        return {}, {}, "请填写密码或 API Token"

    async def fetch(self) -> dict:
        svc = self.config
        raw_url = (svc.get("url") or "").strip().rstrip("/")
        if not raw_url:
            return {"enabled": True, "status": "error", "error": "未填写 URL"}

        # Auto fix missing protocol or port 800 -> 8006
        if not raw_url.startswith(("http://", "https://")):
            raw_url = "https://" + raw_url
        if raw_url.endswith(":800"):
            raw_url = raw_url + "6"

        base = raw_url
        client = self._get_client()

        for attempt in range(2):
            headers, cookies, auth_err = await self._get_auth_headers_and_cookies(client, base)
            if auth_err:
                return {"enabled": True, "status": "auth_failed", "error": auth_err}

            try:
                # 1. Auto-discover active nodes if node is unknown/default
                specified_node = (svc.get("node") or "").strip()
                active_node = specified_node

                nodes_resp = await client.get(f"{base}/api2/json/nodes", headers=headers, cookies=cookies, timeout=6)
                if nodes_resp.status_code == 401:
                    # Token invalid or ticket expired
                    self._ticket = ""
                    if attempt == 0 and svc.get("password"):
                        continue
                    return {"enabled": True, "status": "auth_failed", "error": "PVE 认证失败 (HTTP 401)"}

                if nodes_resp.status_code == 200:
                    node_list = nodes_resp.json().get("data", [])
                    available_nodes = [n.get("node") for n in node_list if n.get("status") == "online"]
                    if available_nodes:
                        if not active_node or active_node not in available_nodes:
                            active_node = available_nodes[0]
                elif nodes_resp.status_code == 403:
                    # Lack of permission on /nodes, use specified node directly
                    if not active_node:
                        active_node = "pve"

                if not active_node:
                    active_node = "pve"

                # 2. Get Node Status
                cpu_usage = 0
                mem_total = 0
                mem_used = 0
                disk_total = 0
                disk_used = 0
                has_node_status = False

                node_resp = await client.get(
                    f"{base}/api2/json/nodes/{active_node}/status",
                    headers=headers,
                    cookies=cookies,
                    timeout=6
                )
                if node_resp.status_code == 200:
                    has_node_status = True
                    node_data = node_resp.json().get("data", {})
                    if "cpu" in node_data:
                        cpu_usage = round(float(node_data["cpu"]) * 100, 1)
                    if "memory" in node_data:
                        mem_total = node_data["memory"].get("total", 0)
                        mem_used = node_data["memory"].get("used", 0)
                    if "rootfs" in node_data:
                        disk_total = node_data["rootfs"].get("total", 0)
                        disk_used = node_data["rootfs"].get("used", 0)

                # 3. Get VMs (QEMU & LXC)
                vms = []
                for vm_type, endpoint in [("qemu", "qemu"), ("lxc", "lxc")]:
                    try:
                        vm_resp = await client.get(
                            f"{base}/api2/json/nodes/{active_node}/{endpoint}",
                            headers=headers,
                            cookies=cookies,
                            timeout=6
                        )
                        if vm_resp.status_code == 200:
                            vm_list = vm_resp.json().get("data", [])
                            for vm in vm_list:
                                vms.append({
                                    "type": vm_type,
                                    "vmid": vm.get("vmid", 0),
                                    "name": vm.get("name", f"{vm_type.upper()} {vm.get('vmid', '?')}"),
                                    "status": vm.get("status", "unknown"),
                                    "cpu": round(float(vm.get("cpu", 0)) * 100, 1),
                                    "mem": round(vm.get("mem", 0) / vm.get("maxmem", 1) * 100, 1) if vm.get("maxmem") else 0,
                                    "mem_used": vm.get("mem", 0),
                                    "mem_total": vm.get("maxmem", 0),
                                    "disk": round(vm.get("disk", 0) / vm.get("maxdisk", 1) * 100, 1) if vm.get("maxdisk") else 0,
                                    "disk_used": vm.get("disk", 0),
                                    "disk_total": vm.get("maxdisk", 0),
                                    "uptime": vm.get("uptime", 0),
                                })
                    except Exception:
                        pass

                if not has_node_status and len(vms) == 0:
                    return {"enabled": True, "status": "error", "error": f"无法获取节点 {active_node} 状态 (HTTP {node_resp.status_code})"}

                mem_pct = round(mem_used / mem_total * 100, 1) if mem_total else 0
                disk_pct = round(disk_used / disk_total * 100, 1) if disk_total else 0

                return {
                    "enabled": True,
                    "status": "ok",
                    "node": active_node,
                    "cpu": cpu_usage,
                    "mem": mem_pct,
                    "mem_used": mem_used,
                    "mem_total": mem_total,
                    "disk": disk_pct,
                    "disk_used": disk_used,
                    "disk_total": disk_total,
                    "vms": vms,
                }

            except Exception as e:
                if attempt == 0 and self._ticket:
                    self._ticket = ""
                    continue
                return {"enabled": True, "status": "error", "error": f"连接异常: {str(e)}"}

        return {"enabled": True, "status": "error", "error": "请求超时或重试失败"}
