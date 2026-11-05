"""This computer's memory, processor and graphics memory, read with the standard library only.

Readings are best-effort: a value that cannot be read is reported as None, never invented.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

_GB = 1024 ** 3
_lock = threading.Lock()
_cpu_last: Optional[tuple] = None
_gpu_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def memory() -> Dict[str, Optional[float]]:
    """Total and free physical memory in GB."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return {"total_gb": None, "free_gb": None}
            return {
                "total_gb": round(status.ullTotalPhys / _GB, 1),
                "free_gb": round(status.ullAvailPhys / _GB, 1),
            }
        if os.path.exists("/proc/meminfo"):
            fields: Dict[str, int] = {}
            with open("/proc/meminfo", "r", encoding="ascii") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    fields[key] = int(rest.split()[0]) * 1024
            free = fields.get("MemAvailable", fields.get("MemFree", 0))
            return {
                "total_gb": round(fields["MemTotal"] / _GB, 1),
                "free_gb": round(free / _GB, 1),
            }
    except Exception:  # noqa: BLE001 - a failed reading is reported as unknown
        pass
    return {"total_gb": None, "free_gb": None}


def _cpu_times() -> Optional[tuple]:
    """(idle, total) processor time since boot, in arbitrary units."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _FileTime(ctypes.Structure):
                _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

            idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
            as_int = lambda ft: (ft.hi << 32) | ft.lo  # noqa: E731
            # Kernel time includes idle time.
            return as_int(idle), as_int(kernel) + as_int(user)
        if os.path.exists("/proc/stat"):
            with open("/proc/stat", "r", encoding="ascii") as fh:
                parts = [int(x) for x in fh.readline().split()[1:]]
            return parts[3] + (parts[4] if len(parts) > 4 else 0), sum(parts)
    except Exception:  # noqa: BLE001
        pass
    return None


def cpu() -> Dict[str, Optional[float]]:
    """Processor use since the previous call (None on the first call)."""
    global _cpu_last
    now = _cpu_times()
    with _lock:
        last, _cpu_last = _cpu_last, now
    percent = None
    if now and last and now[1] > last[1]:
        busy = 1.0 - (now[0] - last[0]) / (now[1] - last[1])
        percent = round(max(0.0, min(1.0, busy)) * 100, 0)
    return {"cores": os.cpu_count(), "percent": percent}


def gpu() -> Optional[Dict[str, Any]]:
    """Graphics memory from nvidia-smi, if it is installed. Read at most about once a second."""
    with _lock:
        if time.time() - _gpu_cache["at"] < 0.9:
            return _gpu_cache["value"]
    value = None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, creationflags=flags,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            name, used, total = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
            value = {
                "name": name,
                "used_gb": round(float(used) / 1024, 1),
                "total_gb": round(float(total) / 1024, 1),
            }
    except Exception:  # noqa: BLE001 - no NVIDIA card, or the tool is missing
        value = None
    with _lock:
        _gpu_cache.update(at=time.time(), value=value)
    return value


def snapshot() -> Dict[str, Any]:
    return {"memory": memory(), "cpu": cpu(), "gpu": gpu()}


class MemoryWatchdog(threading.Thread):
    """Stops working sessions when free memory falls below the user's floor.

    The stop is cooperative: the agent halts at its next step, it does not kill a command that is
    already running.
    """

    def __init__(self, app, interval: float = 2.0):
        super().__init__(name="memory-watchdog", daemon=True)
        self.app = app
        self.interval = interval
        self.tripped = 0

    def check_once(self) -> bool:
        floor = float(self.app.settings.get("min_free_ram_gb") or 0)
        if floor <= 0:
            return False
        free = memory()["free_gb"]
        if free is None or free >= floor:
            return False
        stopped = False
        for session in list(self.app.sessions.values()):
            if session.status in ("working", "permission"):
                session.cancel()
                session.bus.emit(
                    "error",
                    {
                        "message": f"Stopped: only {free} GB of memory was free, below your "
                        f"limit of {floor:g} GB. Nothing else was changed. You can raise the "
                        "limit in Settings, Hardware.",
                        "kind": "memory",
                    },
                )
                stopped = True
        if stopped:
            self.tripped += 1
        return stopped

    def run(self) -> None:
        while True:
            time.sleep(self.interval)
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - the watchdog must never die
                pass
