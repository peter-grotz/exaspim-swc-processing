"""Tests for :mod:`exaspim_swc_processing.stage`."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from aind_data_schema.core.processing import DataProcess
from aind_data_schema_models.process_names import ProcessName

from exaspim_swc_processing.stage import (
    DATA_PROCESS_FILENAME,
    build_stage_process,
    resolve_code,
    write_stage_process,
)

CAPSULE_ID = "7f4c2aad-f244-4c08-b443-80156da0ed4a"
COMMIT = "0d5ba138e7c9b7282d845e4dc6a8e2d213215716"
START = datetime(2026, 8, 19, 22, 10, 2, tzinfo=timezone.utc)


def test_code_url_names_the_capsule_that_actually_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old helper hardcoded slug 7989393 while running as capsule 4319239."""
    monkeypatch.setenv("CO_CAPSULE_ID", CAPSULE_ID)
    monkeypatch.delenv("CODE_VERSION", raising=False)
    code = resolve_code("exaspim-swc-transform")
    assert code.url.endswith(CAPSULE_ID)


def test_code_version_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned commit is recorded, not whatever the remote HEAD is at run time."""
    monkeypatch.setenv("CO_CAPSULE_ID", CAPSULE_ID)
    monkeypatch.setenv("CODE_VERSION", COMMIT)
    assert resolve_code("exaspim-swc-transform").version == COMMIT


def test_a_github_url_is_preferred_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """AIND prefers a repository URL over a Code Ocean one."""
    monkeypatch.setenv("CO_CAPSULE_ID", CAPSULE_ID)
    code = resolve_code("x", url="https://github.com/peter-grotz/exaspim-swc-processing")
    assert code.url == "https://github.com/peter-grotz/exaspim-swc-processing"


def test_version_is_none_rather_than_empty_when_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent version is null, not an empty string that looks recorded."""
    monkeypatch.setenv("CO_CAPSULE_ID", CAPSULE_ID)
    monkeypatch.delenv("CODE_VERSION", raising=False)
    assert resolve_code("x").version is None


def test_url_is_empty_outside_code_ocean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running locally, with no capsule id and no url, leaves the url blank."""
    monkeypatch.delenv("CO_CAPSULE_ID", raising=False)
    assert resolve_code("x").url == ""


def test_language_version_reports_the_running_interpreter() -> None:
    """The recorded interpreter is the one that ran, not a pinned string."""
    import sys

    expected = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    assert resolve_code("x").language_version == expected


def test_stage_process_records_parameters_and_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime parameters and output facts both reach the record."""
    monkeypatch.setenv("CO_CAPSULE_ID", CAPSULE_ID)
    process = build_stage_process(
        "exaspim_swc_transform",
        resolve_code("exaspim-swc-transform"),
        start_time=START,
        output_path="alignment",
        parameters={"swc_dir": "/data/refinement/final-world"},
        output_parameters={"transformed": 58, "input": 58},
    )
    assert process.code.parameters.model_dump()["swc_dir"] == "/data/refinement/final-world"
    assert process.output_parameters.model_dump()["transformed"] == 58
    assert str(process.output_path) == "alignment"


def test_allocated_cpus_are_preferred_over_the_containers_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CO_CPUS describes what the task was allocated, which is the useful number."""
    monkeypatch.setenv("CO_CPUS", "16")
    process = build_stage_process("s", resolve_code("c"), START, "out")
    assert process.resources.cpu_cores == 16


def test_a_non_numeric_cpu_allocation_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed CO_CPUS does not break the record."""
    monkeypatch.setenv("CO_CPUS", "many")
    process = build_stage_process("s", resolve_code("c"), START, "out")
    assert process.resources.cpu_cores is not None


def test_required_resource_fields_are_never_null() -> None:
    """The schema requires os and architecture; the old helper could emit None."""
    process = build_stage_process("s", resolve_code("c"), START, "out")
    assert process.resources.os
    assert process.resources.architecture


def test_other_process_type_carries_notes() -> None:
    """``OTHER`` requires a name and non-empty notes."""
    process = build_stage_process(
        "packaging",
        resolve_code("c"),
        START,
        "out",
        process_type=ProcessName.OTHER,
        notes="Regrouped outputs.",
    )
    assert process.process_type == "Other"
    assert process.notes == "Regrouped outputs."


def test_end_time_defaults_to_now() -> None:
    """A stage that does not pass an end time still records one."""
    process = build_stage_process("s", resolve_code("c"), START, "out")
    assert process.end_date_time >= START


def test_write_round_trips_through_the_schema(tmp_path: Path) -> None:
    """The written file is what the terminal stage will read back."""
    process = build_stage_process("s", resolve_code("c"), START, "out")
    path = write_stage_process(process, tmp_path / "alignment")
    assert path.name == DATA_PROCESS_FILENAME
    reloaded = DataProcess.model_validate_json(path.read_text(encoding="utf-8"))
    assert reloaded.name == "s"
