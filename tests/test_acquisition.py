"""Tests for :mod:`exaspim_swc_processing.acquisition`."""

import json
from pathlib import Path

import pytest

from exaspim_swc_processing.acquisition import (
    ACQUISITION_FIELD,
    ACQUISITION_KEY,
    AcquisitionNotFoundError,
    acquisition_sources,
    resolve_acquisition,
)
from exaspim_swc_processing.parent_metadata import MetadataSource

DATASET = "exaSPIM_826509_2026-05-12_18-03-50_processed_2026-05-22_14-04-22"
DOCUMENT = {"axes": [{"dimension": 0, "direction": "Superior_to_inferior"}]}


def _source(value: object) -> object:
    """Return a fetcher yielding a fixed value.

    Parameters
    ----------
    value : object
        Document to return, or ``None`` for a source with no record.

    Returns
    -------
    object
        A fetcher.
    """
    return lambda _name: value


def _raising(_name: str) -> None:
    """Stand in for a source that cannot be reached.

    Parameters
    ----------
    _name : str
        Asset name, ignored.

    Raises
    ------
    RuntimeError
        Always.
    """
    raise RuntimeError("endpoint down")


def test_the_registry_is_preferred_over_s3(tmp_path: Path) -> None:
    """DocDB holds what the indexer published; a staged copy may be stale."""
    sources = [
        (MetadataSource.DOCDB_V2, _source(DOCUMENT)),
        (MetadataSource.S3, _source({"axes": "wrong"})),
    ]
    path, source = resolve_acquisition(DATASET, tmp_path / "acq.json", sources)
    assert source is MetadataSource.DOCDB_V2
    assert json.loads(path.read_text(encoding="utf-8")) == DOCUMENT


def test_v1_answers_when_v2_has_no_record(tmp_path: Path) -> None:
    """Most exaSPIM assets are not in the v2 index."""
    sources = [
        (MetadataSource.DOCDB_V2, _source(None)),
        (MetadataSource.DOCDB_V1, _source(DOCUMENT)),
    ]
    _, source = resolve_acquisition(DATASET, tmp_path / "acq.json", sources)
    assert source is MetadataSource.DOCDB_V1


def test_s3_answers_for_an_unindexed_asset(tmp_path: Path) -> None:
    """The object at the dataset root is the last registry-side source."""
    sources = [
        (MetadataSource.DOCDB_V2, _source(None)),
        (MetadataSource.S3, _source(DOCUMENT)),
    ]
    _, source = resolve_acquisition(DATASET, tmp_path / "acq.json", sources)
    assert source is MetadataSource.S3


def test_an_unreachable_source_is_skipped(tmp_path: Path) -> None:
    """A DocDB outage must not stop a transform that S3 can serve."""
    sources = [
        (MetadataSource.DOCDB_V2, _raising),
        (MetadataSource.S3, _source(DOCUMENT)),
    ]
    _, source = resolve_acquisition(DATASET, tmp_path / "acq.json", sources)
    assert source is MetadataSource.S3


def test_the_staged_copy_is_the_last_resort(tmp_path: Path) -> None:
    """A mounted bundle still works when nothing is published."""
    staged = tmp_path / "staged.json"
    staged.write_text(json.dumps(DOCUMENT), encoding="utf-8")
    path, source = resolve_acquisition(
        DATASET, tmp_path / "acq.json", [(MetadataSource.S3, _source(None))], str(staged)
    )
    assert path == staged
    assert source is None


def test_no_acquisition_anywhere_is_an_error(tmp_path: Path) -> None:
    """Transforming without the axis orientation would mis-register silently."""
    with pytest.raises(AcquisitionNotFoundError, match="No acquisition"):
        resolve_acquisition(DATASET, tmp_path / "acq.json", [(MetadataSource.S3, _source(None))])


def test_a_missing_staged_copy_is_not_used(tmp_path: Path) -> None:
    """A path that does not exist is not a fallback."""
    with pytest.raises(AcquisitionNotFoundError):
        resolve_acquisition(
            DATASET,
            tmp_path / "acq.json",
            [(MetadataSource.S3, _source(None))],
            str(tmp_path / "absent.json"),
        )


def test_the_default_order_is_v2_then_v1_then_s3() -> None:
    """The registry is authoritative; S3 answers only for unindexed assets."""
    assert [source for source, _ in acquisition_sources()] == [
        MetadataSource.DOCDB_V2,
        MetadataSource.DOCDB_V1,
        MetadataSource.S3,
    ]


def test_the_field_and_object_names_match_the_schema() -> None:
    """DocDB nests the acquisition under a field; S3 publishes it as a file."""
    assert ACQUISITION_FIELD == "acquisition"
    assert ACQUISITION_KEY == "acquisition.json"
