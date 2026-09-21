"""Assemble the AIND ``processing`` record for a single reconstruction.

Every stage of the pipeline writes a ``data_process.json`` describing what it did. Those
records cover the whole run, not one cell, so this module collects them into a
:class:`~aind_data_schema.core.processing.Processing` for each cell directory.

Three constraints the schema imposes, all enforced here rather than discovered at write
time:

* ``DataProcess.name`` must be unique within a record. The field defaults to ``""`` and is
  auto-filled from ``process_type``, so records that never set it collide.
* ``dependency_graph`` keys must be exactly the set of process names.
* ``DataProcess.pipeline_name`` must name an entry in ``Processing.pipelines``.

``Processing`` also re-sorts ``data_processes`` by ``start_date_time`` on validation and
appends a note when it does. The graph is keyed by name, so it survives that reordering.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aind_data_schema.components.identifiers import Code
from aind_data_schema.core.processing import DataProcess, Processing


class ProcessingAssemblyError(ValueError):
    """Raised when a per-cell processing record cannot be assembled."""


def linear_dependency_graph(processes: Sequence[DataProcess]) -> dict[str, list[str]]:
    """Build a dependency graph chaining processes in chronological order.

    The pipeline is a linear chain, so each process depends on the one before it. Pass an
    explicit graph instead if a stage ever fans out.

    Ordering is taken from ``start_date_time`` rather than the order the caller supplied.
    :class:`~aind_data_schema.core.processing.Processing` re-sorts ``data_processes``
    chronologically on validation, so a graph built from the caller's order would silently
    disagree with the record it is attached to, describing a chain that runs backwards.

    Parameters
    ----------
    processes : Sequence[DataProcess]
        Processes to chain, in any order.

    Returns
    -------
    dict[str, list[str]]
        Mapping of each process name to the names of its inputs. The earliest has none.
    """
    graph: dict[str, list[str]] = {}
    previous: str | None = None
    for process in sorted(processes, key=lambda item: item.start_date_time):
        graph[process.name] = [previous] if previous is not None else []
        previous = process.name
    return graph


def _require_unique_names(processes: Sequence[DataProcess]) -> None:
    """Reject a process sequence containing duplicate names.

    Parameters
    ----------
    processes : Sequence[DataProcess]
        Processes to check.

    Raises
    ------
    ProcessingAssemblyError
        If two processes share a name. The schema reports this as a bare
        ``data_processes must have unique names``; this names the offenders.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for process in processes:
        if process.name in seen and process.name not in duplicates:
            duplicates.append(process.name)
        seen.add(process.name)
    if duplicates:
        raise ProcessingAssemblyError(
            "data_processes must have unique names; duplicated: " + ", ".join(sorted(duplicates))
        )


def build_cell_processing(
    processes: Sequence[DataProcess],
    pipeline: Code,
    dependency_graph: Mapping[str, Sequence[str]] | None = None,
    notes: str | None = None,
) -> Processing:
    """Assemble the processing record published alongside one cell.

    Parameters
    ----------
    processes : Sequence[DataProcess]
        The pipeline's stage records, in execution order. Each is tagged with
        ``pipeline.name`` so it validates against ``Processing.pipelines``.
    pipeline : Code
        The pipeline that produced the cell: repository URL, name, and semantic version.
        AIND requires the version here to agree with ``nextflow.config`` and the
        ``PIPELINE_*`` environment variables.
    dependency_graph : Mapping[str, Sequence[str]] | None, optional
        Explicit graph of process name to input process names. Defaults to a linear chain
        in the order of ``processes``.
    notes : str | None, optional
        Free-text notes recorded on the processing record.

    Returns
    -------
    Processing
        The assembled record, with ``pipelines`` and ``dependency_graph`` populated.

    Raises
    ------
    ProcessingAssemblyError
        If ``processes`` is empty, if two processes share a name, or if an explicit
        ``dependency_graph`` does not cover exactly the given processes.
    """
    if not processes:
        raise ProcessingAssemblyError("At least one DataProcess is required")
    _require_unique_names(processes)

    tagged = [process.model_copy(update={"pipeline_name": pipeline.name}) for process in processes]
    graph = (
        {name: list(inputs) for name, inputs in dependency_graph.items()}
        if dependency_graph is not None
        else linear_dependency_graph(tagged)
    )

    names = {process.name for process in tagged}
    if set(graph) != names:
        missing = sorted(names - set(graph))
        unknown = sorted(set(graph) - names)
        raise ProcessingAssemblyError(
            "dependency_graph must cover exactly the given processes; "
            f"missing: {missing}, unknown: {unknown}"
        )

    return Processing(
        data_processes=tagged,
        pipelines=[pipeline],
        dependency_graph=graph,
        notes=notes,
    )
