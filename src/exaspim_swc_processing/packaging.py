"""Write each cell directory as a self-contained AIND derived data asset.

Ties the other modules together: :mod:`layout` regroups the stage outputs by cell,
:mod:`naming` and :mod:`data_description` name and describe each one, and :mod:`processing`
records how it was made. This module drives them and writes the result to disk.

Each cell directory receives exactly two metadata files, ``data_description.json`` and
``processing.json``. That is the complete required set for a ``processing``-type derived
asset: ``REQUIRED_FILE_SETS`` in aind-data-schema only demands ``data_description``
alongside ``processing``. ``subject``, ``procedures``, ``acquisition`` and ``instrument``
are deliberately not inherited — including ``subject`` would pull in a four-file
requirement that the upstream assets cannot currently satisfy, so the asset would advertise
itself as incomplete. Quality control belongs to a later stage and is not written here.

The packaging step records itself as a :class:`~aind_data_schema.core.processing.DataProcess`
appended to each cell's record. It is a real processing step — it is what produced the
directory — and it carries the provenance of where the parent metadata was read from.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aind_data_schema.components.identifiers import Code
from aind_data_schema.core.processing import DataProcess, ProcessStage
from aind_data_schema_models.process_names import ProcessName

from exaspim_swc_processing.data_description import (
    DataDescriptionDerivationError,
    derive_cell_data_description,
)
from exaspim_swc_processing.layout import ARTIFACT_SPECS, ArtifactSpec, discover_cells
from exaspim_swc_processing.layout import write_cell_directory as _write_cell_directory
from exaspim_swc_processing.naming import ReconstructionId
from exaspim_swc_processing.parent_metadata import ParentMetadata
from exaspim_swc_processing.processing import build_cell_processing

logger = logging.getLogger(__name__)

PACKAGING_STEP_NAME = "exaspim_swc_packaging"
"""``DataProcess.name`` of the step this module performs."""

DEFAULT_EXPERIMENTERS = ("Peter Grotz",)
"""Default ``DataProcess.experimenters`` for the packaging step."""

DATA_DESCRIPTION_FILENAME = "data_description.json"
PROCESSING_FILENAME = "processing.json"


@dataclass(frozen=True)
class PackagedCell:
    """One cell directory that was written.

    Attributes
    ----------
    reconstruction : ReconstructionId
        Identifier of the cell.
    directory : Path
        Directory written.
    asset_name : str
        Name of the derived asset, which is also the directory name.
    """

    reconstruction: ReconstructionId
    directory: Path
    asset_name: str


@dataclass(frozen=True)
class SkippedCell:
    """One cell that could not be packaged.

    Attributes
    ----------
    reconstruction : ReconstructionId
        Identifier of the cell.
    reason : str
        Why it was skipped, for the run summary.
    """

    reconstruction: ReconstructionId
    reason: str


@dataclass(frozen=True)
class PackagingResult:
    """Outcome of packaging a run.

    Attributes
    ----------
    packaged : tuple[PackagedCell, ...]
        Cells written.
    skipped : tuple[SkippedCell, ...]
        Cells not written, with reasons. A skipped cell is distinguishable from a cell
        that produced nothing, which matters when a run reports success.
    """

    packaged: tuple[PackagedCell, ...]
    skipped: tuple[SkippedCell, ...]


def build_packaging_process(
    parent: ParentMetadata,
    code: Code,
    start_time: datetime,
    end_time: datetime,
    output_path: str,
    cell_count: int,
    experimenters: Sequence[str] = DEFAULT_EXPERIMENTERS,
) -> DataProcess:
    """Describe the packaging step itself.

    Parameters
    ----------
    parent : ParentMetadata
        Resolved parent metadata, supplying the provenance of where it was read from.
    code : Code
        The packaging code: repository URL, name and version.
    start_time : datetime
        When packaging started.
    end_time : datetime
        When packaging finished.
    output_path : str
        Where the cell directories were written, relative to ``/results``.
    cell_count : int
        Number of cells packaged in the run.
    experimenters : Sequence[str], optional
        Who is responsible for the run, by default :data:`DEFAULT_EXPERIMENTERS`.

    Returns
    -------
    DataProcess
        The step record, carrying :meth:`ParentMetadata.provenance` in
        ``output_parameters``. Typed ``OTHER`` because regrouping files into per-cell
        assets is not one of the vocabulary's defined operations; the docs direct
        unmatched operations to ``ANALYSIS`` or ``OTHER``, and ``OTHER`` requires the
        ``name`` and ``notes`` set here.
    """
    return DataProcess(
        process_type=ProcessName.OTHER,
        name=PACKAGING_STEP_NAME,
        stage=ProcessStage.PROCESSING,
        code=code,
        experimenters=list(experimenters),
        start_date_time=start_time,
        end_date_time=end_time,
        output_path=output_path,
        output_parameters={**parent.provenance(), "cells_packaged": cell_count},
        notes="Regrouped pipeline outputs into one derived asset per reconstruction.",
    )


def package_cells(
    stage_root: Path,
    output_root: Path,
    parent: ParentMetadata,
    stage_processes: Sequence[DataProcess],
    pipeline: Code,
    creation_time: datetime,
    describe_packaging: Callable[[int, datetime], DataProcess],
    specs: Sequence[ArtifactSpec] = ARTIFACT_SPECS,
    overrides: Mapping[str, object] | None = None,
) -> PackagingResult:
    """Write every discovered cell as its own derived data asset.

    Parameters
    ----------
    stage_root : Path
        Root holding the pipeline stage outputs.
    output_root : Path
        Directory the cell directories are created in.
    parent : ParentMetadata
        Parent asset metadata and the source it came from.
    stage_processes : Sequence[DataProcess]
        The pipeline's stage records, shared by every cell.
    pipeline : Code
        The pipeline that produced the run.
    creation_time : datetime
        Creation time shared by every cell in the run.
    describe_packaging : Callable[[int, datetime], DataProcess]
        Builds this packaging step's record, given the number of cells written and the
        time the work finished. A callable rather than a record because both values are
        only known once discovery has run, yet the record is embedded in every cell.
    specs : Sequence[ArtifactSpec], optional
        Artifacts to collect, by default :data:`~exaspim_swc_processing.layout.ARTIFACT_SPECS`.
    overrides : Mapping[str, object] | None, optional
        ``DataDescription`` fields to set explicitly on every cell, overriding the parent.

    Returns
    -------
    PackagingResult
        Cells written and cells skipped. A cell whose description cannot be derived is
        skipped rather than written without metadata.
    """
    specs = tuple(specs)
    skipped: list[SkippedCell] = []

    # Resolve every cell before writing any: the packaging record embedded in each cell
    # states how many the run produced, which is not known until the last is resolved.
    planned = []
    for reconstruction, cell in discover_cells(stage_root, specs).items():
        missing = cell.missing_roles(specs)
        if missing:
            logger.warning(
                "Cell %s is missing required artifact(s): %s",
                reconstruction.stem,
                ", ".join(missing),
            )
        try:
            description = derive_cell_data_description(
                parent.data_description,
                reconstruction,
                creation_time,
                tags=parent.tags(),
                **(overrides or {}),
            )
        except DataDescriptionDerivationError as error:
            logger.warning("Skipping %s: %s", reconstruction.stem, error)
            skipped.append(SkippedCell(reconstruction, str(error)))
            continue
        planned.append((reconstruction, cell, description))

    finished = datetime.now(timezone.utc)
    processes = [*stage_processes, describe_packaging(len(planned), finished)]
    record = build_cell_processing(processes, pipeline)

    packaged: list[PackagedCell] = []
    for reconstruction, cell, description in planned:
        cell_dir = output_root / description.name
        _write_cell_directory(cell, cell_dir, specs)
        (cell_dir / DATA_DESCRIPTION_FILENAME).write_text(
            description.model_dump_json(indent=2), encoding="utf-8"
        )
        (cell_dir / PROCESSING_FILENAME).write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )
        packaged.append(PackagedCell(reconstruction, cell_dir, description.name))

    logger.info("Packaged %d cell(s), skipped %d", len(packaged), len(skipped))
    return PackagingResult(packaged=tuple(packaged), skipped=tuple(skipped))
