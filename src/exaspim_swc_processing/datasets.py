"""Find a processed imaging dataset through the metadata registry.

The transform needs to know which processed dataset a run's registration belongs to: its
name, the bucket it lives in, and its subject. The registry holds all three on the
record -- ``name``, ``location`` and ``subject.subject_id`` -- so they are read from
there rather than parsed out of names, which would tie the pipeline to one naming
convention.

A caller may identify the dataset by name, by S3 URI, by a mounted path that contains
the name, or by subject id alone. A subject can have several records (raw and processed),
so candidates are narrowed to those that carry a CCF registration, confirmed by checking
for its record at a known key -- a lookup, not a search.

The registry is tried first, DocDB v2 then v1. Only when neither answers is S3 consulted,
and then only by exact name -- taken from the URI or path as given -- with the subject read
from the dataset's own ``subject.json``. Nothing here assumes a name prefix or format. A bare
subject id the registry does not know is refused: finding a dataset in S3 from a subject
alone would mean guessing at how datasets are named.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlparse

from exaspim_swc_processing import sources
from exaspim_swc_processing.parent_metadata import MetadataSource
from exaspim_swc_processing.sources import DOCDB_HOST, DocDbClient, S3Client

REGISTRATION_KEY = "ccf_alignment/processing.json"
"""Key, below a dataset, of the CCF registration's own record."""

PROJECTION = {"name": 1, "location": 1, "subject.subject_id": 1, "created": 1}
"""Record fields a lookup needs."""

LIMIT = 50
"""Most records considered for a single subject."""

SUBJECT_FILES = ("subject.json", "data_description.json")
"""Files at a dataset root that name its subject, in the order tried."""


class DatasetNotFoundError(LookupError):
    """Raised when a dataset specification resolves to nothing usable."""


class DatasetMismatchError(ValueError):
    """Raised when the dataset given disagrees with the image the reconstructions name."""


@dataclass(frozen=True)
class ProcessedDataset:
    """A processed dataset as the registry describes it.

    Attributes
    ----------
    name : str
        Dataset name, which is also its key prefix in the bucket.
    bucket : str
        Bucket holding it.
    subject_id : str
        Subject the data was acquired from.
    created : str
        When the record was created, used to prefer the most recent.
    source : str
        Where this was resolved from, e.g. ``"docdb_v1"``.
    """

    name: str
    bucket: str
    subject_id: str
    created: str
    source: str


def dataset_from_image_path(image_path: str) -> tuple[str, str] | None:
    """Recover the bucket and dataset name from a recorded image URI.

    A reconstruction asset records the image it was traced on, e.g.
    ``s3://aind-open-data/<dataset>/fusion/fused.zarr``. The dataset is the first key
    segment, whatever its name looks like.

    Parameters
    ----------
    image_path : str
        An ``s3://`` URI.

    Returns
    -------
    tuple[str, str] | None
        ``(bucket, dataset)``, or ``None`` if the URI names no dataset.
    """
    parsed = urlparse(image_path.strip())
    key = parsed.path.lstrip("/")
    if parsed.scheme != "s3" or not parsed.netloc or not key:
        return None
    return parsed.netloc, key.split("/")[0]


def registry_sources(host: str = DOCDB_HOST) -> list[tuple[MetadataSource, DocDbClient]]:
    """Build the registry endpoints in the order they are consulted.

    Parameters
    ----------
    host : str, optional
        DocDB host, by default :data:`~exaspim_swc_processing.sources.DOCDB_HOST`.

    Returns
    -------
    list[tuple[MetadataSource, DocDbClient]]
        DocDB v2, then v1.
    """
    return [
        (MetadataSource.DOCDB_V2, sources._docdb_client("v2", host)),
        (MetadataSource.DOCDB_V1, sources._docdb_client("v1", host)),
    ]


def candidate_names(spec: str) -> list[str]:
    """List the dataset names a specification could refer to.

    Parameters
    ----------
    spec : str
        A dataset name, an ``s3://`` URI, or a path that contains the name.

    Returns
    -------
    list[str]
        Candidates to look up by exact name, most specific first.
    """
    cleaned = spec.strip().strip("'\"").rstrip("/")
    if not cleaned:
        return []
    if cleaned.startswith("s3://"):
        key = urlparse(cleaned).path.lstrip("/")
        return [key.split("/")[0]] if key else []
    parts = [part for part in PurePosixPath(cleaned).parts if part not in ("/", "")]
    return list(dict.fromkeys(reversed(parts)))


def dataset_from_record(record: dict, source: MetadataSource) -> ProcessedDataset | None:
    """Build a :class:`ProcessedDataset` from a registry record.

    Parameters
    ----------
    record : dict
        A record with ``name``, ``location`` and ``subject.subject_id``.
    source : MetadataSource
        Which endpoint it came from.

    Returns
    -------
    ProcessedDataset | None
        The dataset, or ``None`` if the record lacks an S3 location or a subject.
    """
    location = urlparse(str(record.get("location") or ""))
    subject = str((record.get("subject") or {}).get("subject_id") or "")
    name = str(record.get("name") or "")
    if location.scheme != "s3" or not location.netloc or not subject or not name:
        return None
    return ProcessedDataset(
        name=name,
        bucket=location.netloc,
        subject_id=subject,
        created=str(record.get("created") or ""),
        source=source.value,
    )


def find_processed_dataset(
    spec: str,
    sources: Sequence[tuple[MetadataSource, DocDbClient]],
    has_registration: Callable[[ProcessedDataset], bool],
) -> ProcessedDataset | None:
    """Resolve a dataset specification through the registry.

    Parameters
    ----------
    spec : str
        A dataset name, ``s3://`` URI, mounted path, or subject id.
    sources : Sequence[tuple[MetadataSource, DocDbClient]]
        Registry endpoints in priority order.
    has_registration : Callable[[ProcessedDataset], bool]
        Whether a dataset carries a CCF registration, typically by checking for
        :data:`REGISTRATION_KEY`.

    Returns
    -------
    ProcessedDataset | None
        The most recent matching dataset that has a registration, or ``None`` when the
        registry has none -- the caller's cue to fall back to S3.
    """
    cleaned = spec.strip().strip("'\"")
    if cleaned.isdigit():
        query: dict = {"subject.subject_id": cleaned}
    else:
        names = candidate_names(cleaned)
        if not names:
            return None
        query = {"name": {"$in": names}}
    for source, client in sources:
        records = client.retrieve_docdb_records(
            filter_query=query, projection=PROJECTION, limit=LIMIT
        )
        found = [
            dataset
            for dataset in (dataset_from_record(record, source) for record in records)
            if dataset is not None and has_registration(dataset)
        ]
        if found:
            return max(found, key=lambda dataset: (dataset.created, dataset.name))
    return None


def _read_json(client: S3Client, bucket: str, key: str) -> dict | None:
    """Read a JSON object from S3, or ``None`` if it is absent or unreadable.

    Parameters
    ----------
    client : S3Client
        An S3 client.
    bucket : str
        Bucket.
    key : str
        Object key.

    Returns
    -------
    dict | None
        The decoded object.
    """
    try:
        return json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:  # noqa: BLE001 - absence is the expected failure
        return None


def registration_exists(client: S3Client) -> Callable[[ProcessedDataset], bool]:
    """Build a check that a dataset carries a CCF registration.

    Parameters
    ----------
    client : S3Client
        An S3 client.

    Returns
    -------
    Callable[[ProcessedDataset], bool]
        True when ``<dataset>/ccf_alignment/processing.json`` can be read.
    """
    return lambda dataset: (
        _read_json(client, dataset.bucket, f"{dataset.name}/{REGISTRATION_KEY}") is not None
    )


def find_in_s3(spec: str, client: S3Client, default_bucket: str) -> ProcessedDataset | None:
    """Resolve a dataset by exact name in S3, reading its subject from its own metadata.

    Parameters
    ----------
    spec : str
        A dataset name, ``s3://`` URI, or path containing the name. A bare subject id is
        not resolvable here and yields ``None``.
    client : S3Client
        An S3 client.
    default_bucket : str
        Bucket to use when the specification does not name one.

    Returns
    -------
    ProcessedDataset | None
        The first candidate that has a registration and names its subject.
    """
    cleaned = spec.strip().strip("'\"")
    if not cleaned or cleaned.isdigit():
        return None
    bucket = urlparse(cleaned).netloc if cleaned.startswith("s3://") else default_bucket
    for name in candidate_names(cleaned):
        if _read_json(client, bucket, f"{name}/{REGISTRATION_KEY}") is None:
            continue
        for filename in SUBJECT_FILES:
            record = _read_json(client, bucket, f"{name}/{filename}") or {}
            subject = str(record.get("subject_id") or "")
            if subject:
                return ProcessedDataset(name, bucket, subject, "", "s3")
    return None


def resolve_processed_dataset(
    spec: str,
    registry: Sequence[tuple[MetadataSource, DocDbClient]],
    client: S3Client,
    default_bucket: str,
    image_path: str = "",
) -> ProcessedDataset:
    """Resolve the processed dataset: registry first, then the traced image, then S3.

    Parameters
    ----------
    spec : str
        What the run was given: a dataset name, ``s3://`` URI, mounted path, subject id,
        or empty.
    registry : Sequence[tuple[MetadataSource, DocDbClient]]
        Registry endpoints in priority order.
    client : S3Client
        An S3 client, for confirming registrations and the fallbacks.
    default_bucket : str
        Bucket for the S3 fallback when the specification names none.
    image_path : str, optional
        The image the reconstructions were traced on, as recorded in their
        ``refinement/data_process.json``.

    Returns
    -------
    ProcessedDataset
        The resolved dataset and where it was resolved from.

    Raises
    ------
    DatasetMismatchError
        If ``spec`` resolves to a different dataset than the one the reconstructions were
        traced on. Transforming them with that registration would be wrong.
    DatasetNotFoundError
        If nothing resolves.
    """
    has_registration = registration_exists(client)
    traced = dataset_from_image_path(image_path) if image_path else None

    # 1. The registry, with whatever the run was given.
    found = find_processed_dataset(spec, registry, has_registration) if spec.strip() else None
    # 2. The image the reconstructions record, looked up by its exact name.
    if found is None and traced is not None:
        bucket, name = traced
        found = find_processed_dataset(name, registry, has_registration) or find_in_s3(
            f"s3://{bucket}/{name}", client, default_bucket
        )
        if found is not None:
            found = ProcessedDataset(
                found.name,
                found.bucket,
                found.subject_id,
                found.created,
                f"{found.source} (from the reconstructions' image_path)",
            )
    # 3. S3, by exact name from what the run was given.
    if found is None and spec.strip():
        found = find_in_s3(spec, client, default_bucket)

    if found is None:
        if spec.strip().strip("'\"").isdigit():
            raise DatasetNotFoundError(
                f"Subject {spec} has no registered processed dataset in DocDB, and the "
                "reconstructions' image_path does not resolve to one. Pass the dataset name "
                "or its s3:// URI; S3 cannot be searched by subject without assuming how "
                "datasets are named."
            )
        raise DatasetNotFoundError(
            f"No processed dataset with a CCF registration resolves from {spec!r} or from "
            f"the reconstructions' image_path {image_path!r}"
        )
    if traced is not None and traced[1] != found.name:
        raise DatasetMismatchError(
            f"The reconstructions were traced on {traced[1]}, but {spec!r} resolves to "
            f"{found.name}. Their registration would be applied to the wrong image."
        )
    return found
