"""Let a pipeline stage record what it did, as an AIND ``DataProcess``.

Each capsule writes a ``data_process.json`` beside its outputs; the terminal stage collects
them into each cell's :class:`~aind_data_schema.core.processing.Processing`. This replaces
the ``processing_metadata.py`` that was duplicated byte-for-byte across two capsules, whose
surrounding helpers had already drifted apart.

It also fixes how provenance was recorded. The old helper hardcoded a capsule slug — the
wrong one, as it happens: the transform capsule recorded slug ``7989393`` while running as
``4319239`` — and filled ``Code.version`` from ``git ls-remote HEAD`` at run time rather
than the commit the pipeline pinned. Both are read from the environment here, so a stage
reports the code that actually ran.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from aind_data_schema.components.identifiers import Code
from aind_data_schema.core.processing import DataProcess, ProcessStage, ResourceUsage
from aind_data_schema_models.process_names import ProcessName

from exaspim_swc_processing.layout import place_artifact

CAPSULE_URL_TEMPLATE = "https://codeocean.allenneuraldynamics.org/capsule/{capsule_id}"
"""Code Ocean capsule URL, used when no repository URL is given."""

DATA_PROCESS_FILENAME = "data_process.json"
"""Name a stage writes its record under."""

UPSTREAM_STAGES = ("dispatch", "refinement", "alignment", "final")
"""Stage directories that are passed along the chain, in pipeline order."""


def _resources() -> ResourceUsage:
    """Describe the machine the stage ran on.

    ``CO_CPUS`` and ``CO_MEMORY`` are exported by the generated Nextflow process bodies
    and describe what the task was *allocated*, which is more useful than what the
    container happens to see.

    Returns
    -------
    ResourceUsage
        The machine description. ``os`` and ``architecture`` are required by the schema,
        so they fall back to non-empty placeholders rather than ``None``.
    """
    allocated = os.environ.get("CO_CPUS")
    return ResourceUsage(
        os=f"{platform.system()} {platform.release()}".strip() or "unknown",
        architecture=platform.machine() or "unknown",
        cpu=platform.processor() or platform.machine() or None,
        cpu_cores=int(allocated) if allocated and allocated.isdigit() else os.cpu_count(),
    )


def installed_version(distribution: str) -> str | None:
    """Return the installed version of a distribution, if it is installed.

    Used as the fallback for ``Code.version``. Code Ocean generates ``main.nf`` and
    exports only ``CO_CAPSULE_ID``, ``CO_CPUS`` and ``CO_MEMORY``, so the pinned capsule
    commit is not available to the running code. The version of the library the capsule
    installed is available, is what actually determines behaviour for these thin
    capsules, and is a value AIND accepts in place of a commit hash.

    Parameters
    ----------
    distribution : str
        Distribution name, e.g. ``"exaspim-swc-processing"``.

    Returns
    -------
    str | None
        The installed version, or ``None`` if the distribution is not installed.
    """
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def resolve_code(
    name: str,
    url: str | None = None,
    version: str | None = None,
    run_script: str = "code/run",
    distribution: str = "exaspim-swc-processing",
) -> Code:
    """Describe the code a stage ran, from the environment.

    Parameters
    ----------
    name : str
        Name of the code, e.g. the repository or capsule name.
    url : str | None, optional
        Repository URL. AIND prefers a GitHub URL; when omitted the Code Ocean capsule
        URL is built from ``CO_CAPSULE_ID``.
    version : str | None, optional
        Version of the code that ran. When omitted, ``CODE_VERSION`` is read, then the
        installed version of ``distribution``. See :func:`installed_version`.
    run_script : str, optional
        Entry point, relative to the repository root, by default ``"code/run"``.
    distribution : str, optional
        Installed package to fall back to for the version, by default this library.

    Returns
    -------
    Code
        The description, recording the capsule that actually ran rather than a hardcoded
        identifier.
    """
    capsule_id = os.environ.get("CO_CAPSULE_ID", "")
    resolved_url = url or (CAPSULE_URL_TEMPLATE.format(capsule_id=capsule_id) if capsule_id else "")
    return Code(
        url=resolved_url,
        name=name,
        version=version or os.environ.get("CODE_VERSION") or installed_version(distribution),
        run_script=Path(run_script),
        language="Python",
        language_version=(
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
    )


def _with_parameters(code: Code, parameters: Mapping[str, object] | None) -> Code:
    """Attach runtime parameters to a :class:`Code`, through validation.

    ``model_copy`` bypasses validation and would leave a plain ``dict`` where the schema
    expects its generic model, so the copy is revalidated.

    Parameters
    ----------
    code : Code
        The code description.
    parameters : Mapping[str, object] | None
        Runtime parameters, or ``None`` to leave the code unchanged.

    Returns
    -------
    Code
        The code, with parameters attached.
    """
    if not parameters:
        return code
    return Code.model_validate({**code.model_dump(), "parameters": dict(parameters)})


def build_stage_process(
    name: str,
    code: Code,
    start_time: datetime,
    output_path: str,
    parameters: Mapping[str, object] | None = None,
    output_parameters: Mapping[str, object] | None = None,
    experimenters: Sequence[str] = (),
    end_time: datetime | None = None,
    process_type: ProcessName = ProcessName.NEURON_SKELETON_PROCESSING,
    notes: str | None = None,
) -> DataProcess:
    """Build the record describing one stage's work.

    Parameters
    ----------
    name : str
        Name of the stage. Must be unique within a run, since
        :class:`~aind_data_schema.core.processing.Processing` rejects duplicates.
    code : Code
        The code that ran, from :func:`resolve_code`.
    start_time : datetime
        When the stage started.
    output_path : str
        Where the stage wrote, relative to ``/results``.
    parameters : Mapping[str, object] | None, optional
        Runtime parameters the stage was given.
    output_parameters : Mapping[str, object] | None, optional
        Facts about what the stage produced, such as counts.
    experimenters : Sequence[str], optional
        Who is responsible for the run.
    end_time : datetime | None, optional
        When the stage finished. Defaults to now.
    process_type : ProcessName, optional
        Operation performed, by default ``NEURON_SKELETON_PROCESSING``.
    notes : str | None, optional
        Free-text notes. Required by the schema when ``process_type`` is ``OTHER``.

    Returns
    -------
    DataProcess
        The stage record, ready to write with :func:`write_stage_process`.
    """
    return DataProcess(
        process_type=process_type,
        name=name,
        stage=ProcessStage.PROCESSING,
        code=_with_parameters(code, parameters),
        experimenters=list(experimenters),
        start_date_time=start_time,
        end_date_time=end_time or datetime.now(timezone.utc),
        output_path=output_path,
        output_parameters=dict(output_parameters) if output_parameters else None,
        notes=notes,
        resources=_resources(),
    )


def write_stage_process(process: DataProcess, output_dir: Path) -> Path:
    """Write a stage record beside the stage's outputs.

    Parameters
    ----------
    process : DataProcess
        The record to write.
    output_dir : Path
        Directory to write into. Created if absent.

    Returns
    -------
    Path
        The file written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / DATA_PROCESS_FILENAME
    path.write_text(process.model_dump_json(indent=2), encoding="utf-8")
    return path


def carry_forward(
    data_dir: Path,
    results_dir: Path,
    stages: Sequence[str],
) -> list[str]:
    """Republish upstream stage outputs so later stages can still see them.

    Nextflow hands each process only the previous one's ``/results``, so a stage that does
    not republish what it received removes it from the chain. The terminal packaging stage
    needs every stage's ``data_process.json`` and the refined reconstructions, so each
    intermediate stage has to pass them along.

    Files are hardlinked where the filesystem allows and copied otherwise, so passing a
    multi-gigabyte tree through costs little beyond directory entries.

    Parameters
    ----------
    data_dir : Path
        Directory the upstream outputs are mounted at.
    results_dir : Path
        Directory this stage writes to.
    stages : Sequence[str]
        Stage directory names to republish, if present.

    Returns
    -------
    list[str]
        The stages that were found and republished, in the order given.
    """
    carried: list[str] = []
    for stage in stages:
        source = data_dir / stage
        if not source.is_dir():
            continue
        for path in sorted(source.rglob("*")):
            if path.is_file():
                place_artifact(path, results_dir / stage / path.relative_to(source))
        carried.append(stage)
    return carried
