import os
import platform
from pathlib import Path

_OS_RELEASE = Path("/etc/os-release")


def _linux_distro() -> str | None:
    if not _OS_RELEASE.exists():
        return None
    try:
        for line in _OS_RELEASE.read_text().splitlines():
            key, _, value = line.partition("=")
            if key == "PRETTY_NAME":
                return value.strip().strip('"')
    except OSError:
        pass
    return None


def _machine_context() -> str:
    """Dynamically describe the local machine (OS/distro/shell/arch) so the
    model doesn't need to be told on every call — recomputed per invocation
    since the same config travels across different machines."""
    os_desc = _linux_distro() or f"{platform.system()} {platform.release()}"
    shell = os.path.basename(os.environ.get("SHELL", "")) or "unknown"
    return f"Machine context: OS={os_desc}; shell={shell}; arch={platform.machine()}."
