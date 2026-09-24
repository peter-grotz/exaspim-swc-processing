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

from exaspim_swc_processing.parent_metadata import MetadataSource
from exaspim_swc_processing.sources import DocDbClient, S3Client

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
) -> ProcessedDataset:
    """Resolve a dataset through the registry first, then S3 by exact name.

    Parameters
    ----------
    spec : str
        A dataset name, ``s3://`` URI, mounted path, or subject id.
    registry : Sequence[tuple[MetadataSource, DocDbClient]]
        Registry endpoints in priority order.
    client : S3Client
        An S3 client, for confirming registrations and the fallback.
    default_bucket : str
        Bucket for the fallback when the specification names none.

    Returns
    -------
    ProcessedDataset
        The resolved dataset and where it was resolved from.

    Raises
    ------
    DatasetNotFoundError
        If neither the registry nor S3 resolves it.
    """
    found = find_processed_dataset(spec, registry, registration_exists(client))
    if found is None:
        found = find_in_s3(spec, client, default_bucket)
    if found is not None:
        return found
    if spec.strip().strip("'\"").isdigit():
        raise DatasetNotFoundError(
            f"Subject {spec} has no registered processed dataset in DocDB. Pass the dataset "
            "name or its s3:// URI instead; S3 cannot be searched by subject without "
            "assuming how datasets are named."
        )
    raise DatasetNotFoundError(
        f"{spec!r} does not resolve to a dataset with a CCF registration in DocDB or in "
        f"s3://{default_bucket}"
    )
