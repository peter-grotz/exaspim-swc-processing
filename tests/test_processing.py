"""Tests for :mod:`exaspim_swc_processing.processing`."""

import json
from pathlib import Path

import pytest
from aind_data_schema.components.identifiers import Code
from aind_data_schema.core.processing import DataProcess, Processing

from exaspim_swc_processing.processing import (
    ProcessingAssemblyError,
    build_cell_processing,
    linear_dependency_graph,
)

FIXTURE = Path(__file__).parent / "resources" / "stage_data_processes.json"
PIPELINE = Code(
    url="https://github.com/peter-grotz/exaspim-swc-processing-pipeline",
    name="exaspim-swc-processing",
    version="0.1.0",
)


def _stage_processes() -> list[DataProcess]:
    """Load the pipeline's four stage records from a real run.

    Returns
    -------
    list[DataProcess]
        The records in execution order.
    """
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [DataProcess.model_validate(record) for record in payload]


def test_fixture_is_the_real_four_stage_pipeline() -> None:
    """The fixture reflects the stages the pipeline actually runs."""
    names = [process.name for process in _stage_processes()]
    assert names == [
        "SWC merge and validation",
        "Neuron skeleton processing",
        "exaspim_swc_transform",
        "aligned_swc_processing",
    ]


def test_dependency_graph_is_populated_and_chained() -> None:
    """The graph records stage order, which every run to date left null."""
    result = build_cell_processing(_stage_processes(), PIPELINE)
    assert result.dependency_graph == {
        "SWC merge and validation": [],
        "Neuron skeleton processing": ["SWC merge and validation"],
        "exaspim_swc_transform": ["Neuron skeleton processing"],
        "aligned_swc_processing": ["exaspim_swc_transform"],
    }


def test_pipeline_is_recorded_on_the_record_and_every_process() -> None:
    """AIND requires the pipeline in ``pipelines`` and referenced by each process."""
    result = build_cell_processing(_stage_processes(), PIPELINE)
    assert [pipeline.name for pipeline in result.pipelines] == ["exaspim-swc-processing"]
    assert result.pipelines[0].version == "0.1.0"
    assert {process.pipeline_name for process in result.data_processes} == {
        "exaspim-swc-processing"
    }


def test_input_processes_are_not_mutated() -> None:
    """Tagging with the pipeline name must not edit the caller's records."""
    processes = _stage_processes()
    build_cell_processing(processes, PIPELINE)
    assert all(process.pipeline_name is None for process in processes)


def test_record_round_trips_through_the_schema() -> None:
    """The assembled record validates as published JSON."""
    result = build_cell_processing(_stage_processes(), PIPELINE)
    reloaded = Processing.model_validate(json.loads(result.model_dump_json()))
    assert reloaded.dependency_graph == result.dependency_graph


def test_notes_are_recorded() -> None:
    """Free-text notes reach the record."""
    result = build_cell_processing(_stage_processes(), PIPELINE, notes="staged for QC")
    assert result.notes is not None
    assert "staged for QC" in result.notes


def test_explicit_dependency_graph_is_used() -> None:
    """A caller can override the linear default, e.g. for a fan-out stage."""
    processes = _stage_processes()
    graph = {
        "SWC merge and validation": [],
        "Neuron skeleton processing": ["SWC merge and validation"],
        "exaspim_swc_transform": ["SWC merge and validation"],
        "aligned_swc_processing": ["Neuron skeleton processing", "exaspim_swc_transform"],
    }
    result = build_cell_processing(processes, PIPELINE, dependency_graph=graph)
    assert result.dependency_graph["aligned_swc_processing"] == [
        "Neuron skeleton processing",
        "exaspim_swc_transform",
    ]


def test_explicit_graph_missing_a_process_is_rejected() -> None:
    """A graph that omits a process would fail schema validation later."""
    processes = _stage_processes()
    graph = {"SWC merge and validation": []}
    with pytest.raises(ProcessingAssemblyError, match="missing"):
        build_cell_processing(processes, PIPELINE, dependency_graph=graph)


def test_explicit_graph_with_an_unknown_process_is_rejected() -> None:
    """A graph naming a process that is not present is a mistake, not a no-op."""
    processes = _stage_processes()
    graph = linear_dependency_graph(processes) | {"not a stage": []}
    with pytest.raises(ProcessingAssemblyError, match="unknown"):
        build_cell_processing(processes, PIPELINE, dependency_graph=graph)


def test_duplicate_names_are_rejected_with_the_offender_named() -> None:
    """``DataProcess.name`` auto-fills from ``process_type``, so collisions are real."""
    processes = _stage_processes()
    clashing = processes[1].model_copy(update={"name": processes[0].name})
    with pytest.raises(ProcessingAssemblyError, match="SWC merge and validation"):
        build_cell_processing([processes[0], clashing], PIPELINE)


def test_empty_process_list_is_rejected() -> None:
    """A cell with no stage records indicates an upstream failure, not an empty record."""
    with pytest.raises(ProcessingAssemblyError, match="At least one"):
        build_cell_processing([], PIPELINE)


def test_linear_dependency_graph_on_a_single_process() -> None:
    """A lone process depends on nothing."""
    processes = _stage_processes()[:1]
    assert linear_dependency_graph(processes) == {"SWC merge and validation": []}
