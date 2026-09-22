"""Tests for :mod:`exaspim_swc_processing.sources`."""

import io
import json
import logging
from pathlib import Path

import pytest
from aind_data_schema.core.data_description import DataDescription

from exaspim_swc_processing.parent_metadata import MetadataSource
from exaspim_swc_processing.sources import (
    CORE_FIELD,
    DOCDB_HOST,
    _docdb_client,
    _s3_client,
    default_sources,
    docdb_fetcher,
    s3_fetcher,
    upgrade_data_description,
)

ASSET = "exaSPIM_794492_2026-01-09_16-50-40_processed_2026-01-17_14-44-16"
FIXTURE = Path(__file__).parent / "resources" / "parent_data_description.json"


def _document() -> dict:
    """Load the real parent ``data_description``.

    Returns
    -------
    dict
        The raw payload.
    """
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class _FakeDocDb:
    """Stands in for ``MetadataDbClient``."""

    def __init__(self, records: list[dict]) -> None:
        """Store the records this client will return.

        Parameters
        ----------
        records : list[dict]
            Records to return from any query.
        """
        self.records = records
        self.calls: list[dict] = []

    def retrieve_docdb_records(self, **kwargs: object) -> list[dict]:
        """Record the query and return the canned records.

        Parameters
        ----------
        **kwargs : object
            Query arguments.

        Returns
        -------
        list[dict]
            The canned records.
        """
        self.calls.append(kwargs)
        return self.records


class _FakeS3:
    """Stands in for a boto3 S3 client."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        """Store the objects this client will serve.

        Parameters
        ----------
        objects : dict[str, bytes]
            Mapping of key to body.
        """
        self.objects = objects
        self.calls: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3 signature
        """Return the object body, or raise if absent.

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
        self.calls.append((Bucket, Key))
        return {"Body": io.BytesIO(self.objects[Key])}


def test_docdb_fetcher_returns_the_core_file() -> None:
    """A record yields its ``data_description``, not the whole document."""
    client = _FakeDocDb([{"name": ASSET, CORE_FIELD: _document()}])
    result = docdb_fetcher("v2", client=client)(ASSET)
    assert result is not None
    assert result["name"] == ASSET


def test_docdb_fetcher_queries_by_name_and_projects_the_core_file() -> None:
    """Only the field needed is requested."""
    client = _FakeDocDb([{"name": ASSET, CORE_FIELD: _document()}])
    docdb_fetcher("v2", client=client)(ASSET)
    assert client.calls[0]["filter_query"] == {"name": ASSET}
    assert client.calls[0]["projection"] == {CORE_FIELD: 1}


def test_docdb_fetcher_returns_none_when_the_endpoint_has_no_record() -> None:
    """An unmigrated asset is absent from v2 and must fall through."""
    assert docdb_fetcher("v2", client=_FakeDocDb([]))(ASSET) is None


def test_docdb_fetcher_warns_when_a_record_lacks_the_core_file(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A record with a null ``data_description`` is reported, not returned empty."""
    client = _FakeDocDb([{"name": ASSET, CORE_FIELD: None}])
    with caplog.at_level(logging.WARNING):
        assert docdb_fetcher("v1", client=client)(ASSET) is None
    assert CORE_FIELD in caplog.text


def test_s3_fetcher_reads_the_asset_prefix() -> None:
    """The asset name is its key prefix."""
    key = f"{ASSET}/data_description.json"
    client = _FakeS3({key: json.dumps(_document()).encode()})
    result = s3_fetcher(client=client)(ASSET)
    assert result is not None
    assert result["name"] == ASSET
    assert client.calls == [("aind-open-data", key)]


def test_s3_fetcher_returns_none_when_the_object_is_absent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing object means the asset is not there, not that the run fails."""
    with caplog.at_level(logging.INFO):
        assert s3_fetcher(client=_FakeS3({}))(ASSET) is None
    assert "No data_description.json" in caplog.text


def test_s3_fetcher_returns_none_on_unreadable_content() -> None:
    """A truncated or non-JSON object is treated as absent."""
    client = _FakeS3({f"{ASSET}/data_description.json": b"not json"})
    assert s3_fetcher(client=client)(ASSET) is None


def test_s3_fetcher_honours_a_custom_bucket() -> None:
    """Private or scratch buckets can be read with the same fetcher."""
    key = f"{ASSET}/data_description.json"
    client = _FakeS3({key: json.dumps(_document()).encode()})
    s3_fetcher(bucket="aind-private-data", client=client)(ASSET)
    assert client.calls == [("aind-private-data", key)]


def test_default_sources_are_ordered_v2_then_v1_then_s3() -> None:
    """Centrally migrated records are preferred over anything done locally."""
    assert [source for source, _ in default_sources()] == [
        MetadataSource.DOCDB_V2,
        MetadataSource.DOCDB_V1,
        MetadataSource.S3,
    ]


def test_upgrade_brings_a_v1_document_to_the_current_schema() -> None:
    """The real 794492 parent upgrades from schema 1.0.4."""
    legacy = {**_document(), "schema_version": "1.0.4", "modalities": None}
    upgraded = upgrade_data_description(legacy)
    assert upgraded["schema_version"].startswith("2.")
    assert upgraded["name"] == ASSET


def test_upgrade_cannot_infer_modalities_without_acquisition() -> None:
    """Documented limitation: modality inference needs acquisition, which is not fetched.

    The result still validates, so the gap is filled through the packaging overrides
    rather than by fetching a core file that cannot be upgraded anyway.
    """
    legacy = {**_document(), "schema_version": "1.0.4", "modalities": None}
    upgraded = upgrade_data_description(legacy)
    assert upgraded["modalities"] == []
    DataDescription.model_validate(upgraded)


def test_lazy_clients_are_constructed_without_network() -> None:
    """Fetchers build their client on first use, not at import."""
    assert _docdb_client("v2", DOCDB_HOST) is not None
    assert _s3_client() is not None
