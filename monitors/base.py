"""Base class for all monitor modules."""

import httpx


class BaseMonitor:
    """Base class for NAS Stats monitor modules.

    Each monitor must:
    1. Accept a config dict in __init__
    2. Implement async fetch() -> dict
    3. Return a dict with at least {"enabled": True, "status": "ok"|"error"|...}
    """

    # Override in subclass to provide a human-readable name
    display_name: str = "Unknown"

    def __init__(self, monitor_id: str, config: dict):
        self.id = monitor_id
        self.config = config
        self.client: httpx.AsyncClient | None = None
        self._logged_in = False

    def set_client(self, client: httpx.AsyncClient):
        """Set the shared httpx async client."""
        self.client = client

    def update_config(self, config: dict):
        """Update the monitor's config at runtime."""
        self.config = config

    async def fetch(self) -> dict:
        """Fetch monitor data. Must be overridden by subclass.

        Returns:
            dict with at least:
                enabled (bool): whether this monitor is active
                status (str): "ok", "error", "auth_failed", "disabled"
                error (str, optional): error message if status != "ok"
        """
        raise NotImplementedError

    async def test_connection(self) -> dict:
        """Test connectivity. Override for custom behavior."""
        try:
            data = await self.fetch()
            ok = data.get("status") == "ok"
            return {"ok": ok, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _url(self, path: str = "") -> str:
        """Build a URL from config['url'] + path."""
        base = (self.config.get("url") or "").rstrip("/")
        return f"{base}{path}" if base else ""
