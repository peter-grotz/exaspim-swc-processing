"""Tests for :mod:`exaspim_swc_processing.resources`."""

import logging
from pathlib import Path

import pytest

from exaspim_swc_processing import resources
from exaspim_swc_processing.resources import (
    cgroup_peak_bytes,
    log_peak_memory,
    memory_limit_bytes,
    peak_memory_report,
    rusage_peak_bytes,
)

GIB = 1024 * 1024 * 1024


def _cgroup(tmp_path: Path, relative: str, value: str) -> Path:
    """Create a fake cgroup counter under a temporary root.

    Parameters
    ----------
    tmp_path : Path
        Temporary root.
    relative : str
        Path of the counter below the root.
    value : str
        File contents.

    Returns
    -------
    Path
        The temporary root.
    """
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return tmp_path


def test_the_v2_counter_is_read(tmp_path: Path) -> None:
    """Cgroup v2 records the peak at ``memory.peak``."""
    root = _cgroup(tmp_path, "sys/fs/cgroup/memory.peak", f"{15 * GIB}\n")
    assert cgroup_peak_bytes(root) == 15 * GIB


def test_the_v1_counter_is_read_when_v2_is_absent(tmp_path: Path) -> None:
    """Older hosts expose ``memory.max_usage_in_bytes`` instead."""
    root = _cgroup(tmp_path, "sys/fs/cgroup/memory/memory.max_usage_in_bytes", f"{3 * GIB}")
    assert cgroup_peak_bytes(root) == 3 * GIB


def test_v2_wins_when_both_exist(tmp_path: Path) -> None:
    """A host exposing both should be read as v2."""
    _cgroup(tmp_path, "sys/fs/cgroup/memory/memory.max_usage_in_bytes", f"{3 * GIB}")
    root = _cgroup(tmp_path, "sys/fs/cgroup/memory.peak", f"{15 * GIB}")
    assert cgroup_peak_bytes(root) == 15 * GIB


def test_no_counter_yields_none(tmp_path: Path) -> None:
    """Off Linux there is nothing to read, which is not an error."""
    assert cgroup_peak_bytes(tmp_path) is None


def test_unparseable_counter_yields_none(tmp_path: Path) -> None:
    """A counter that is not a number must not crash a stage."""
    root = _cgroup(tmp_path, "sys/fs/cgroup/memory.peak", "max\n")
    assert cgroup_peak_bytes(root) is None


def test_rusage_peak_is_positive() -> None:
    """The fallback reports this process's own usage."""
    assert rusage_peak_bytes() > 0


def test_the_allocation_is_read_from_the_environment() -> None:
    """``main.nf`` exports CO_MEMORY in bytes."""
    assert memory_limit_bytes({"CO_MEMORY": str(128849018880)}) == 128849018880


def test_a_missing_allocation_yields_none() -> None:
    """Outside the pipeline there is nothing to compare against."""
    assert memory_limit_bytes({}) is None


def test_an_unparseable_allocation_yields_none() -> None:
    """A malformed value is not a measurement."""
    assert memory_limit_bytes({"CO_MEMORY": "lots"}) is None


def test_the_report_compares_peak_to_the_allocation(tmp_path: Path) -> None:
    """The 120 GB run exists to produce this line."""
    root = _cgroup(tmp_path, "sys/fs/cgroup/memory.peak", str(15 * GIB))
    report = peak_memory_report(root, {"CO_MEMORY": str(120 * GIB)})
    assert report == "peak 15360 MiB of 122880 MiB allocated (12%), from cgroup"


def test_the_report_names_its_source_when_falling_back(tmp_path: Path) -> None:
    """A reader must know the rusage number undercounts concurrent children."""
    report = peak_memory_report(tmp_path, {"CO_MEMORY": str(120 * GIB)})
    assert "undercounts" in report


def test_the_report_works_without_an_allocation(tmp_path: Path) -> None:
    """Run outside the pipeline, the peak is still worth printing."""
    root = _cgroup(tmp_path, "sys/fs/cgroup/memory.peak", str(2 * GIB))
    assert peak_memory_report(root, {}) == (
        "peak 2048 MiB (no CO_MEMORY to compare against), from cgroup"
    )


def test_a_zero_allocation_is_not_divided_by(tmp_path: Path) -> None:
    """CO_MEMORY=0 must not raise ZeroDivisionError."""
    root = _cgroup(tmp_path, "sys/fs/cgroup/memory.peak", str(2 * GIB))
    assert "no CO_MEMORY" in peak_memory_report(root, {"CO_MEMORY": "0"})


def test_the_peak_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The stage records the measurement in its run log."""
    with caplog.at_level(logging.INFO, logger=resources.__name__):
        log_peak_memory()
    assert "peak" in caplog.text


def test_a_failed_measurement_does_not_end_the_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Losing a whole transform to a reporting bug would be absurd."""

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("no /proc here")

    monkeypatch.setattr(resources, "peak_memory_report", _boom)
    with caplog.at_level(logging.INFO, logger=resources.__name__):
        log_peak_memory()
    assert "Could not measure peak memory" in caplog.text
