"""Report what a stage actually used, so its allocation can be set from evidence.

The pipeline's CPU and memory were inherited rather than measured, and the figures in
issue #13 are arithmetic over file headers, not observations. A stage that logs its own
peak lets the next allocation come from a number instead.

The measurement has to come from the control group, not from :func:`resource.getrusage`.
The resample stage runs its JVM in a child process, so ``RUSAGE_SELF`` misses it
entirely, and ``RUSAGE_CHILDREN`` reports the largest single child rather than the
container total. The cgroup counter is what the scheduler itself accounts against, which
is the number the allocation has to cover.
"""

from __future__ import annotations

import logging
import os
import resource
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

CGROUP_PEAK_PATHS = (
    "/sys/fs/cgroup/memory.peak",
    "/sys/fs/cgroup/memory/memory.max_usage_in_bytes",
)
"""Where the kernel records peak usage, cgroup v2 first then v1."""

MEMORY_LIMIT_VAR = "CO_MEMORY"
"""Environment variable the generated ``main.nf`` exports, in bytes."""

_MIB = 1024 * 1024


def cgroup_peak_bytes(root: Path | None = None) -> int | None:
    """Read the container's peak memory from the cgroup counter.

    Parameters
    ----------
    root : Path | None, optional
        Filesystem root to resolve the cgroup paths against. Defaults to ``/``.

    Returns
    -------
    int | None
        Peak bytes, or ``None`` when no counter is readable -- which is the normal case
        off Linux.
    """
    base = root or Path("/")
    for candidate in CGROUP_PEAK_PATHS:
        path = base / candidate.lstrip("/")
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def rusage_peak_bytes() -> int:
    """Return the peak RSS of this process and its largest child.

    This undercounts a stage whose children run concurrently, so it is only a fallback
    for :func:`cgroup_peak_bytes`.

    Returns
    -------
    int
        Peak bytes.
    """
    # ru_maxrss is kilobytes on Linux and bytes on macOS.
    scale = 1 if sys.platform == "darwin" else 1024
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return (own + children) * scale


def memory_limit_bytes(environ: dict[str, str] | None = None) -> int | None:
    """Return the memory the scheduler allocated to this stage.

    Parameters
    ----------
    environ : dict[str, str] | None, optional
        Environment to read. Defaults to the process environment.

    Returns
    -------
    int | None
        Allocated bytes, or ``None`` when the variable is absent or unparseable.
    """
    raw = (environ if environ is not None else os.environ).get(MEMORY_LIMIT_VAR, "")
    try:
        return int(raw)
    except ValueError:
        return None


def peak_memory_report(root: Path | None = None, environ: dict[str, str] | None = None) -> str:
    """Summarise peak usage against the allocation, for the run log.

    Parameters
    ----------
    root : Path | None, optional
        Filesystem root for the cgroup counter.
    environ : dict[str, str] | None, optional
        Environment supplying the allocation.

    Returns
    -------
    str
        A line such as ``"peak 14832 MiB of 122880 MiB allocated (12%), from cgroup"``.
    """
    peak = cgroup_peak_bytes(root)
    source = "cgroup"
    if peak is None:
        peak = rusage_peak_bytes()
        source = "rusage, undercounts concurrent children"

    limit = memory_limit_bytes(environ)
    if limit:
        share = f" of {limit // _MIB} MiB allocated ({round(100 * peak / limit)}%)"
    else:
        share = f" (no {MEMORY_LIMIT_VAR} to compare against)"
    return f"peak {peak // _MIB} MiB{share}, from {source}"


def log_peak_memory() -> None:
    """Log peak usage against the allocation.

    Call at the end of a stage. Never raises: a stage must not fail over a measurement.
    """
    try:
        logger.info("%s", peak_memory_report())
    except Exception as error:  # noqa: BLE001 - reporting must not end a run
        logger.info("Could not measure peak memory: %s", type(error).__name__)
