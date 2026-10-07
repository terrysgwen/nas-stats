"""Home Assistant monitor module."""

import httpx
from .base import BaseMonitor


class HomeAssistantMonitor(BaseMonitor):
    display_name = "Home Assistant"

    async def fetch(self) -> dict:
        url = self.config.get("url")
        if not url:
            return {"enabled": True, "status": "error", "error": "未配置 URL"}

        token = self.config.get("token", "")
        headers = {
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        client = self.client or httpx.AsyncClient(timeout=8)

        try:
            # 1. API status
            status_resp = await client.get(self._url("/api/"), headers=headers, timeout=5)
            if status_resp.status_code == 401:
                return {"enabled": True, "status": "auth_failed", "error": "Long-Lived Access Token 无效"}

            if status_resp.status_code != 200:
                return {"enabled": True, "status": "error", "error": f"HTTP {status_resp.status_code}"}

            # 2. States inspection
            states_resp = await client.get(self._url("/api/states"), headers=headers, timeout=8)
            if states_resp.status_code != 200:
                return {"enabled": True, "status": "error", "error": f"HTTP {states_resp.status_code}"}

            states = states_resp.json()
            total_entities = len(states)

            # Categorize entities
            sensors_count = 0
            lights_on = 0
            lights_total = 0
            switches_on = 0
            switches_total = 0
            automations_count = 0
            unavailable_count = 0

            for ent in states:
                entity_id = ent.get("entity_id", "")
                state = ent.get("state", "")

                if state == "unavailable":
                    unavailable_count += 1

                domain = entity_id.split(".")[0] if "." in entity_id else ""
                if domain == "sensor" or domain == "binary_sensor":
                    sensors_count += 1
                elif domain == "light":
                    lights_total += 1
                    if state == "on":
                        lights_on += 1
                elif domain == "switch":
                    switches_total += 1
                    if state == "on":
                        switches_on += 1
                elif domain == "automation":
                    automations_count += 1

            return {
                "enabled": True,
                "status": "ok",
                "total_entities": total_entities,
                "lights_on": lights_on,
                "lights_total": lights_total,
                "switches_on": switches_on,
                "switches_total": switches_total,
                "sensors_count": sensors_count,
                "automations_count": automations_count,
                "unavailable_count": unavailable_count,
            }
        except Exception as e:
            return {"enabled": True, "status": "error", "error": str(e)}
