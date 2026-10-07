"""Monitor module registry.

Provides MONITOR_REGISTRY mapping monitor IDs to their classes,
and a helper to instantiate monitors from database config.
"""

from .base import BaseMonitor
from .qbittorrent import QBittorrentMonitor
from .transmission import TransmissionMonitor
from .ikuai import IKuaiMonitor
from .pve import PVEMonitor
from .moviepilot import MoviePilotMonitor
from .istoreos import IStoreOSMonitor
from .emby import EmbyMonitor
from .plex import PlexMonitor
from .jellyfin import JellyfinMonitor
from .homeassistant import HomeAssistantMonitor
from .easynvr import EasyNVRMonitor

MONITOR_REGISTRY: dict[str, type[BaseMonitor]] = {
    "qbittorrent": QBittorrentMonitor,
    "transmission": TransmissionMonitor,
    "ikuai": IKuaiMonitor,
    "pve": PVEMonitor,
    "moviepilot": MoviePilotMonitor,
    "istoreos": IStoreOSMonitor,
    "emby": EmbyMonitor,
    "plex": PlexMonitor,
    "jellyfin": JellyfinMonitor,
    "homeassistant": HomeAssistantMonitor,
    "easynvr": EasyNVRMonitor,
}

__all__ = [
    "BaseMonitor",
    "MONITOR_REGISTRY",
    "QBittorrentMonitor",
    "TransmissionMonitor",
    "IKuaiMonitor",
    "PVEMonitor",
    "MoviePilotMonitor",
    "IStoreOSMonitor",
    "EmbyMonitor",
    "PlexMonitor",
    "JellyfinMonitor",
    "HomeAssistantMonitor",
    "EasyNVRMonitor",
]
