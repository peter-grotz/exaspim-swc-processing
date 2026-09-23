"""Obtain a dataset's ``acquisition`` from the registry, falling back to S3.

The transform needs the acquisition's axis orientation, and ``RegistrationPipeline``
takes it as a file path. Where that file comes from matters: a dataset's mounted bundle
may carry a stale copy, or none, while the registry holds the record the indexer
published.

The order matches :func:`~exaspim_swc_processing.sources.default_sources` -- DocDB v2,
then v1, then the object in S3 -- so the registry is authoritative and S3 answers only
for assets it has not indexed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from exaspim_swc_processing.parent_metadata import MetadataSource
from exaspim_swc_processing.sources import (
    DOCDB_HOST,
    OPEN_DATA_BUCKET,
    Fetcher,
    docdb_fetcher,
    s3_fetcher,
)

logger = logging.getLogger(__name__)

ACQUISITION_FIELD = "acquisition"
"""Top-level field of a DocDB record holding the acquisition."""

ACQUISITION_KEY = "acquisition.json"
"""Object at the asset root holding the acquisition."""


class AcquisitionNotFoundError(LookupError):
    """Raised when no source holds an acquisition for the dataset."""


def acquisition_sources(
    host: str = DOCDB_HOST, bucket: str = OPEN_DATA_BUCKET
) -> list[tuple[MetadataSource, Fetcher]]:
    """Build the resolution order for an acquisition.

    Parameters
    ----------
    host : str, optional
        DocDB host, by default :data:`~exaspim_swc_processing.sources.DOCDB_HOST`.
    bucket : str, optional
        S3 bucket, by default :data:`~exaspim_swc_processing.sources.OPEN_DATA_BUCKET`.

    Returns
    -------
    list[tuple[MetadataSource, Fetcher]]
        Sources in priority order: DocDB v2, DocDB v1, then S3.
    """
    return [
        (MetadataSource.DOCDB_V2, docdb_fetcher("v2", host, field=ACQUISITION_FIELD)),
        (MetadataSource.DOCDB_V1, docdb_fetcher("v1", host, field=ACQUISITION_FIELD)),
        (MetadataSource.S3, s3_fetcher(bucket, filename=ACQUISITION_KEY)),
    ]


def resolve_acquisition(
    asset_name: str,
    destination: Path,
    sources: Sequence[tuple[MetadataSource, Fetcher]] | None = None,
    local_fallback: str = "",
) -> tuple[Path, MetadataSource | None]:
    """Write the dataset's acquisition to a file and say where it came from.

    Parameters
    ----------
    asset_name : str
        Processed dataset name.
    destination : Path
        File to write the acquisition to when it comes from a registry or S3.
    sources : Sequence[tuple[MetadataSource, Fetcher]] | None, optional
        Sources in priority order. Defaults to :func:`acquisition_sources`.
    local_fallback : str, optional
        An acquisition already on disk, used only when no source answers.

    Returns
    -------
    tuple[Path, MetadataSource | None]
        The file to read, and the source it came from -- ``None`` when the local
        fallback was used.

    Raises
    ------
    AcquisitionNotFoundError
        If no source answers and there is no usable local fallback.
    """
    for source, fetch in sources if sources is not None else acquisition_sources():
        try:
            document = fetch(asset_name)
        except Exception as error:  # noqa: BLE001 - an unreachable source is not fatal
            logger.info("Acquisition source %s failed: %s", source.value, error)
            continue
        if not document:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document, indent=2), encoding="utf-8")
        logger.info("Acquisition for %s read from %s", asset_name, source.value)
        return destination, source

    if local_fallback and Path(local_fallback).is_file():
        logger.warning(
            "No acquisition for %s in DocDB or S3; using the copy staged at %s",
            asset_name,
            local_fallback,
        )
        return Path(local_fallback), None
    raise AcquisitionNotFoundError(
        f"No acquisition for {asset_name} in DocDB v2, DocDB v1 or S3, and no local copy"
    )
