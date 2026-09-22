"""Concrete fetchers for the parent asset's ``data_description``.

:mod:`parent_metadata` defines the resolution order and provenance but takes its sources as
injected callables so the logic can be tested without network access. This module supplies
the real ones: the DocDB v2 and v1 endpoints, and the S3 bucket the indexer reads.

Clients are constructed lazily and can be passed in, so nothing here opens a connection at
import time.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Protocol

from exaspim_swc_processing.parent_metadata import Fetcher, MetadataSource

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

DOCDB_HOST = "api.allenneuraldynamics.org"
"""Host serving the AIND metadata index."""

OPEN_DATA_BUCKET = "aind-open-data"
"""Public bucket holding released AIND assets."""

DATA_DESCRIPTION_KEY = "data_description.json"
"""Name of the core file fetched, at the asset prefix."""

CORE_FIELD = "data_description"
"""Field of a DocDB record holding the core file."""


class DocDbClient(Protocol):
    """The part of ``MetadataDbClient`` this module uses."""

    def retrieve_docdb_records(self, **kwargs: object) -> list[dict]:
        """Return records matching a query.

        Parameters
        ----------
        **kwargs : object
            Query arguments.

        Returns
        -------
        list[dict]
            Matching records.
        """
        ...  # pragma: no cover


class S3Client(Protocol):
    """The part of a boto3 S3 client this module uses."""

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3 signature
        """Return an object from a bucket.

        Parameters
        ----------
        Bucket : str
            Bucket name.
        Key : str
            Object key.

        Returns
        -------
        dict
            A response with a readable ``Body``.
        """
        ...  # pragma: no cover


def _docdb_client(version: str, host: str) -> DocDbClient:
    """Build a DocDB client for one endpoint.

    Parameters
    ----------
    version : str
        API version, ``"v1"`` or ``"v2"``.
    host : str
        Host serving the index.

    Returns
    -------
    DocDbClient
        A ``MetadataDbClient``. Imported lazily so the dependency is only needed when a
        DocDB source is actually used.
    """
    from aind_data_access_api.document_db import MetadataDbClient

    return MetadataDbClient(host=host, version=version)


def docdb_fetcher(
    version: str, host: str = DOCDB_HOST, client: DocDbClient | None = None
) -> Fetcher:
    """Build a fetcher reading one DocDB endpoint.

    The endpoints are two views of the same assets rather than two populations: an asset
    that has been migrated appears in v2 at its upgraded schema and in v1 at its original
    one. An unmigrated asset appears only in v1.

    Parameters
    ----------
    version : str
        API version, ``"v1"`` or ``"v2"``.
    host : str, optional
        Host serving the index, by default :data:`DOCDB_HOST`.
    client : DocDbClient | None, optional
        A pre-built client. Constructed on first use when omitted.

    Returns
    -------
    Fetcher
        Returns the asset's raw ``data_description``, or ``None`` if the endpoint has no
        record for it.
    """

    def _fetch(asset_name: str) -> dict | None:
        """Read one asset's ``data_description`` from the endpoint.

        Parameters
        ----------
        asset_name : str
            Name of the asset.

        Returns
        -------
        dict | None
            The raw document, or ``None`` if absent.
        """
        resolved = client if client is not None else _docdb_client(version, host)
        records = resolved.retrieve_docdb_records(
            filter_query={"name": asset_name},
            projection={CORE_FIELD: 1},
            limit=1,
        )
        if not records:
            return None
        document = records[0].get(CORE_FIELD)
        if not document:
            logger.warning(
                "DocDB %s has a record for %r with no %s", version, asset_name, CORE_FIELD
            )
            return None
        return document

    return _fetch


def _s3_client() -> S3Client:
    """Build an anonymous S3 client.

    ``aind-open-data`` is public, and the Nextflow task bodies export no AWS credentials,
    so unsigned access is both sufficient and independent of role propagation.

    Returns
    -------
    S3Client
        A boto3 S3 client configured for unsigned requests. Imported lazily.
    """
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config

    return boto3.client("s3", config=Config(signature_version=UNSIGNED))


def s3_fetcher(
    bucket: str = OPEN_DATA_BUCKET,
    client: S3Client | None = None,
) -> Fetcher:
    """Build a fetcher reading the asset's ``data_description.json`` from S3.

    This is the source the indexer itself reads, so it answers for assets that have not
    been registered yet.

    Parameters
    ----------
    bucket : str, optional
        Bucket holding the asset, by default :data:`OPEN_DATA_BUCKET`.
    client : S3Client | None, optional
        A pre-built S3 client. Constructed on first use when omitted.

    Returns
    -------
    Fetcher
        Returns the raw ``data_description``, or ``None`` if the object is absent or is
        not readable as JSON.
    """

    def _fetch(asset_name: str) -> dict | None:
        """Read one asset's ``data_description.json`` from the bucket.

        Parameters
        ----------
        asset_name : str
            Name of the asset, which is also its key prefix.

        Returns
        -------
        dict | None
            The raw document, or ``None`` if absent or unreadable.
        """
        resolved = client if client is not None else _s3_client()
        key = f"{asset_name}/{DATA_DESCRIPTION_KEY}"
        try:
            response = resolved.get_object(Bucket=bucket, Key=key)
            return json.loads(response["Body"].read())
        except Exception as error:  # noqa: BLE001 - any read failure means "not here"
            logger.info("No %s in s3://%s/%s (%s)", DATA_DESCRIPTION_KEY, bucket, key, error)
            return None

    return _fetch


def default_sources(
    host: str = DOCDB_HOST,
    bucket: str = OPEN_DATA_BUCKET,
) -> list[tuple[MetadataSource, Fetcher]]:
    """Build the standard resolution order.

    DocDB v2 first, because a record there was migrated centrally and needs nothing done
    to it. Then v1, whose document is upgraded locally. Then S3, for assets that are not
    registered at all.

    Parameters
    ----------
    host : str, optional
        DocDB host, by default :data:`DOCDB_HOST`.
    bucket : str, optional
        S3 bucket, by default :data:`OPEN_DATA_BUCKET`.

    Returns
    -------
    list[tuple[MetadataSource, Fetcher]]
        Sources in priority order, ready for
        :func:`~exaspim_swc_processing.parent_metadata.resolve_parent_metadata`.
    """
    return [
        (MetadataSource.DOCDB_V2, docdb_fetcher("v2", host)),
        (MetadataSource.DOCDB_V1, docdb_fetcher("v1", host)),
        (MetadataSource.S3, s3_fetcher(bucket)),
    ]


def upgrade_data_description(document: dict) -> dict:
    """Upgrade a ``data_description`` document to the current schema.

    Wraps ``aind-metadata-upgrader``, which expects a whole metadata record rather than a
    bare core file, so the document is wrapped in the minimal envelope it needs. Full
    record validation is skipped: the other core files are absent here, and for exaSPIM
    they would fail anyway because ``acquisition.instrument_id`` is empty.

    Parameters
    ----------
    document : dict
        Raw ``data_description`` payload at any schema version.

    Returns
    -------
    dict
        The upgraded payload.

    Notes
    -----
    ``modalities`` comes back empty for a v1 document. The upgrader infers it from
    ``acquisition``, which is not fetched here, so a v1 parent yields
    ``modalities: []``. The field still validates, but supply it through the packaging
    ``overrides`` to publish something meaningful.
    """
    from aind_metadata_upgrader.upgrade import Upgrade

    envelope = {
        "name": document.get("name", ""),
        "location": document.get("name", ""),
        CORE_FIELD: document,
    }
    return Upgrade(envelope, skip_metadata_validation=True).upgrade_core_file(CORE_FIELD)


__all__: Sequence[str] = [
    "DOCDB_HOST",
    "OPEN_DATA_BUCKET",
    "default_sources",
    "docdb_fetcher",
    "s3_fetcher",
    "upgrade_data_description",
]
