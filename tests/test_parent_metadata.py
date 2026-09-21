"""Tests for :mod:`exaspim_swc_processing.parent_metadata`."""

import json
from pathlib import Path

import pytest

from exaspim_swc_processing.parent_metadata import (
    MetadataSource,
    ParentMetadata,
    ParentMetadataNotFoundError,
    resolve_parent_metadata,
)

FIXTURE = Path(__file__).parent / "resources" / "parent_data_description.json"
ASSET = "exaSPIM_794492_2026-01-09_16-50-40_processed_2026-01-17_14-44-16"


def _document(**overrides: object) -> dict:
    """Load the real parent document, already upgraded to v2.

    Parameters
    ----------
    **overrides : object
        Fields to override.

    Returns
    -------
    dict
        The raw ``data_description`` payload.
    """
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(overrides)
    return payload


def _legacy_document() -> dict:
    """Return the parent document as it appears before upgrading.

    Returns
    -------
    dict
        The payload with a v1 ``schema_version``.
    """
    return _document(schema_version="1.0.4")


def _upgrade(document: dict) -> dict:
    """Stand in for ``aind-metadata-upgrader``.

    Parameters
    ----------
    document : dict
        The document to upgrade.

    Returns
    -------
    dict
        The document at schema 2.4.1.
    """
    return {**document, "schema_version": "2.4.1"}


def _found(document: dict) -> object:
    """Build a fetcher that always returns ``document``.

    Parameters
    ----------
    document : dict
        Document to return.

    Returns
    -------
    object
        The fetcher.
    """
    return lambda name: document


def _absent(name: str) -> None:
    """Stand in for a source holding no record.

    Parameters
    ----------
    name : str
        Asset name, ignored.

    Returns
    -------
    None
        Always.
    """
    return None


def test_docdb_v2_is_preferred_and_needs_no_upgrade() -> None:
    """A v2 record is used as-is."""
    result = resolve_parent_metadata(
        ASSET,
        [
            (MetadataSource.DOCDB_V2, _found(_document())),
            (MetadataSource.S3, _found(_legacy_document())),
        ],
        _upgrade,
    )
    assert result.source is MetadataSource.DOCDB_V2
    assert result.upgraded is False
    assert result.source_schema_version == "2.4.1"


def test_falls_back_to_docdb_v1_and_upgrades() -> None:
    """A v1 record is upgraded and the upgrade is recorded."""
    result = resolve_parent_metadata(
        ASSET,
        [
            (MetadataSource.DOCDB_V2, _absent),
            (MetadataSource.DOCDB_V1, _found(_legacy_document())),
        ],
        _upgrade,
    )
    assert result.source is MetadataSource.DOCDB_V1
    assert result.upgraded is True
    assert result.source_schema_version == "1.0.4"
    assert result.data_description.schema_version == "2.4.1"


def test_falls_back_to_s3_when_neither_index_has_it() -> None:
    """Unregistered assets still resolve, which is the current exaSPIM case."""
    result = resolve_parent_metadata(
        ASSET,
        [
            (MetadataSource.DOCDB_V2, _absent),
            (MetadataSource.DOCDB_V1, _absent),
            (MetadataSource.S3, _found(_legacy_document())),
        ],
        _upgrade,
    )
    assert result.source is MetadataSource.S3
    assert result.upgraded is True


def test_sources_are_tried_in_order() -> None:
    """Later sources are not consulted once one answers."""
    consulted: list[str] = []

    def _record(source: str) -> object:
        """Build a fetcher that records that it was consulted.

        Parameters
        ----------
        source : str
            Label to record.

        Returns
        -------
        object
            The fetcher.
        """

        def _fetch(name: str) -> dict:
            """Record the call and answer.

            Parameters
            ----------
            name : str
                Asset name, ignored.

            Returns
            -------
            dict
                The document.
            """
            consulted.append(source)
            return _document()

        return _fetch

    resolve_parent_metadata(
        ASSET,
        [(MetadataSource.DOCDB_V2, _record("v2")), (MetadataSource.S3, _record("s3"))],
        _upgrade,
    )
    assert consulted == ["v2"]


def test_missing_everywhere_raises_and_names_what_was_tried() -> None:
    """A sample with no metadata anywhere must be skipped, not silently published."""
    with pytest.raises(ParentMetadataNotFoundError) as info:
        resolve_parent_metadata(
            ASSET,
            [(MetadataSource.DOCDB_V2, _absent), (MetadataSource.S3, _absent)],
            _upgrade,
        )
    assert ASSET in str(info.value)
    assert "docdb_v2" in str(info.value)
    assert "s3" in str(info.value)


def test_provenance_records_the_fallback_for_output_parameters() -> None:
    """The packaging step's output_parameters carry where metadata came from."""
    result = resolve_parent_metadata(
        ASSET,
        [(MetadataSource.DOCDB_V2, _absent), (MetadataSource.S3, _found(_legacy_document()))],
        _upgrade,
    )
    assert result.provenance() == {
        "parent_asset_name": ASSET,
        "metadata_source": "s3",
        "parent_schema_version": "1.0.4",
        "upgraded_to": "2.4.1",
    }


def test_provenance_reports_no_upgrade_for_a_v2_source() -> None:
    """``upgraded_to`` is null when nothing was upgraded."""
    result = resolve_parent_metadata(
        ASSET, [(MetadataSource.DOCDB_V2, _found(_document()))], _upgrade
    )
    assert result.provenance()["upgraded_to"] is None


def test_tags_make_fallback_assets_findable() -> None:
    """Tags are indexed, so these are how affected assets get re-derived later."""
    result = resolve_parent_metadata(
        ASSET, [(MetadataSource.S3, _found(_legacy_document()))], _upgrade
    )
    assert result.tags() == ["metadata-source:s3", "metadata-upgraded"]


def test_tags_omit_the_upgrade_marker_when_not_upgraded() -> None:
    """A v2 source is tagged with its origin only."""
    result = resolve_parent_metadata(
        ASSET, [(MetadataSource.DOCDB_V2, _found(_document()))], _upgrade
    )
    assert result.tags() == ["metadata-source:docdb_v2"]


def test_absent_schema_version_is_reported_as_none() -> None:
    """A document with no ``schema_version`` is treated as needing an upgrade."""
    document = _document()
    del document["schema_version"]
    result = resolve_parent_metadata(ASSET, [(MetadataSource.S3, _found(document))], _upgrade)
    assert result.source_schema_version is None
    assert result.upgraded is True


def test_parent_metadata_is_frozen() -> None:
    """The resolved record is a value object."""
    result = resolve_parent_metadata(
        ASSET, [(MetadataSource.DOCDB_V2, _found(_document()))], _upgrade
    )
    assert isinstance(result, ParentMetadata)
    with pytest.raises(AttributeError):
        result.source = MetadataSource.S3  # type: ignore[misc]


def test_a_future_schema_is_left_alone() -> None:
    """A document newer than the current major is not pushed through the upgrader."""
    result = resolve_parent_metadata(
        ASSET, [(MetadataSource.DOCDB_V2, _found(_document(schema_version="3.0.0")))], _upgrade
    )
    assert result.upgraded is False
    assert result.source_schema_version == "3.0.0"


def test_an_unparseable_schema_version_is_treated_as_old() -> None:
    """A malformed version is upgraded rather than trusted."""
    result = resolve_parent_metadata(
        ASSET, [(MetadataSource.S3, _found(_document(schema_version="unknown")))], _upgrade
    )
    assert result.upgraded is True
