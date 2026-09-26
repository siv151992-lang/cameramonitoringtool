"""Probes that ask a camera whether it is alive and whether its SD card is healthy."""

from camera_monitor.probes.base import StorageInfo, StorageState

__all__ = ["StorageInfo", "StorageState"]
