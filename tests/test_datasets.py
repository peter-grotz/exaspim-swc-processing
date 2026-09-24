"""Tests for :mod:`exaspim_swc_processing.datasets`."""

import io
import json

import pytest

from exaspim_swc_processing.datasets import (
    DatasetNotFoundError,
    ProcessedDataset,
    candidate_names,
    dataset_from_record,
    find_in_s3,
    find_processed_dataset,
    registration_exists,
    resolve_processed_dataset,
)
from exaspim_swc_processing.parent_metadata import MetadataSource

BUCKET = "aind-open-data"
NAME = "exaSPIM_841260_2026-07-07_15-13-51_processed_2026-07-26_09-25-11"
RAW = "exaSPIM_841260_2026-07-07_15-13-51"
ODD = "someLab-841260-processed"


def _record(name: str, created: str = "2026-08-23", subject: str = "841260") -> dict:
    """Build a registry record.

    Parameters
    ----------
    name : str
        Dataset name.
    created : str, optional
        Creation date.
    subject : str, optional
        Subject id.

    Returns
    -------
    dict
        A record shaped like DocDB's.
    """
    return {
        "name": name,
        "location": f"s3://{BUCKET}/{name}",
        "subject": {"subject_id": subject},
        "created": created,
    }


class _Registry:
    """A DocDB client answering from fixed records, recording each query."""

    def __init__(self, records: list[dict]) -> None:
        """Hold the records.

        Parameters
        ----------
        records : list[dict]
            Records to match against.
        """
        self.records = records
        self.queries: list[dict] = []

    def retrieve_docdb_records(self, **kwargs: object) -> list[dict]:
        """Return records matching a name or subject query.

        Parameters
        ----------
        **kwargs : object
            Includes ``filter_query``.

        Returns
        -------
        list[dict]
            Matching records.
        """
        query = kwargs["filter_query"]
        self.queries.append(query)
        if "subject.subject_id" in query:
            return [
                r for r in self.records if r["subject"]["subject_id"] == query["subject.subject_id"]
            ]
        return [r for r in self.records if r["name"] in query["name"]["$in"]]


class _S3:
    """An S3 client serving a fixed set of JSON objects."""

    def __init__(self, objects: dict[str, dict]) -> None:
        """Hold the objects.

        Parameters
        ----------
        objects : dict[str, dict]
            ``"bucket/key"`` to JSON content.
        """
        self.objects = objects

    def get_object(self, **kwargs: object) -> dict:
        """Return an object's body; a missing key fails, as boto3 does.

        Parameters
        ----------
        **kwargs : object
            ``Bucket`` and ``Key``.

        Returns
        -------
        dict
            The object, with a readable ``Body``.
        """
        payload = self.objects[f"{kwargs['Bucket']}/{kwargs['Key']}"]
        return {"Body": io.BytesIO(json.dumps(payload).encode())}


def _registered(*names: str, subject: dict | None = None) -> _S3:
    """Build an S3 client where the given datasets carry a registration.

    Parameters
    ----------
    *names : str
        Datasets with ``ccf_alignment/processing.json``.
    subject : dict | None, optional
        ``subject.json`` content to place beside each.

    Returns
    -------
    _S3
        The client.
    """
    objects = {f"{BUCKET}/{n}/ccf_alignment/processing.json": {} for n in names}
    for n in names:
        if subject is not None:
            objects[f"{BUCKET}/{n}/subject.json"] = subject
    return _S3(objects)


V1 = MetadataSource.DOCDB_V1
V2 = MetadataSource.DOCDB_V2


def test_an_s3_uri_yields_its_dataset_name() -> None:
    """A URI may point below the dataset root; only the first key segment is the name."""
    assert candidate_names(f"s3://{BUCKET}/{NAME}/ccf_alignment/") == [NAME]


def test_an_s3_uri_with_no_key_yields_nothing() -> None:
    """A bare bucket names no dataset."""
    assert candidate_names(f"s3://{BUCKET}/") == []


def test_a_mounted_path_yields_each_component_innermost_first() -> None:
    """The dataset may sit anywhere in a mount path; no naming convention is assumed."""
    assert candidate_names(f"/data/{NAME}/ccf_alignment") == ["ccf_alignment", NAME, "data"]


def test_a_blank_specification_yields_nothing() -> None:
    """Nothing to look up."""
    assert candidate_names("  ") == []


def test_a_record_becomes_a_dataset() -> None:
    """Name, bucket and subject come from the record, not from parsing the name."""
    dataset = dataset_from_record(_record(ODD), V1)
    assert dataset == ProcessedDataset(ODD, BUCKET, "841260", "2026-08-23", "docdb_v1")


@pytest.mark.parametrize(
    "record",
    [
        {**_record(NAME), "location": "/local/path"},
        {**_record(NAME), "subject": {}},
        {**_record(NAME), "name": ""},
    ],
)
def test_an_incomplete_record_is_not_a_dataset(record: dict) -> None:
    """Without an S3 location, a subject and a name there is nothing to fetch."""
    assert dataset_from_record(record, V1) is None


def test_a_name_resolves_through_the_registry() -> None:
    """The registry answers by exact name."""
    found = find_processed_dataset(
        NAME, [(V1, _Registry([_record(NAME)]))], registration_exists(_registered(NAME))
    )
    assert found is not None and found.name == NAME and found.source == "docdb_v1"


def test_a_subject_picks_the_dataset_that_has_a_registration() -> None:
    """A subject's raw record has no registration; its processed one does."""
    registry = _Registry([_record(RAW, "2026-07-08"), _record(NAME, "2026-08-23")])
    found = find_processed_dataset(
        "841260", [(V1, registry)], registration_exists(_registered(NAME))
    )
    assert found is not None and found.name == NAME


def test_the_most_recent_registered_dataset_wins() -> None:
    """Two processed datasets for a subject: prefer the newer."""
    older, newer = f"{ODD}-a", f"{ODD}-b"
    registry = _Registry([_record(older, "2026-01-01"), _record(newer, "2026-06-01")])
    found = find_processed_dataset(
        "841260", [(V1, registry)], registration_exists(_registered(older, newer))
    )
    assert found is not None and found.name == newer


def test_v2_is_tried_before_v1() -> None:
    """The migrated index is authoritative when it answers."""
    v2, v1 = _Registry([_record(NAME)]), _Registry([_record(NAME)])
    found = find_processed_dataset(
        NAME, [(V2, v2), (V1, v1)], registration_exists(_registered(NAME))
    )
    assert found is not None and found.source == "docdb_v2"
    assert v1.queries == []


def test_v1_answers_when_v2_has_nothing() -> None:
    """Most exaSPIM assets are only in v1."""
    found = find_processed_dataset(
        NAME,
        [(V2, _Registry([])), (V1, _Registry([_record(NAME)]))],
        registration_exists(_registered(NAME)),
    )
    assert found is not None and found.source == "docdb_v1"


def test_an_unregistered_or_unnamed_specification_finds_nothing() -> None:
    """No candidates, no query."""
    registry = _Registry([_record(NAME)])
    assert find_processed_dataset("", [(V1, registry)], registration_exists(_S3({}))) is None
    assert registry.queries == []


def test_s3_resolves_an_exact_name_with_any_naming() -> None:
    """The fallback takes the name as given; the subject comes from subject.json."""
    found = find_in_s3(ODD, _registered(ODD, subject={"subject_id": "841260"}), BUCKET)
    assert found == ProcessedDataset(ODD, BUCKET, "841260", "", "s3")


def test_s3_reads_the_subject_from_data_description_when_subject_json_is_absent() -> None:
    """Either core file names the subject."""
    client = _registered(ODD)
    client.objects[f"{BUCKET}/{ODD}/data_description.json"] = {"subject_id": "841260"}
    assert find_in_s3(ODD, client, BUCKET).subject_id == "841260"


def test_s3_uses_the_bucket_a_uri_names() -> None:
    """A URI's bucket overrides the default."""
    client = _S3({
        f"other/{ODD}/ccf_alignment/processing.json": {},
        f"other/{ODD}/subject.json": {"subject_id": "7"},
    })
    assert find_in_s3(f"s3://other/{ODD}/", client, BUCKET).bucket == "other"


def test_s3_skips_a_candidate_without_a_registration_or_subject() -> None:
    """A registered dataset that names no subject cannot be used."""
    assert find_in_s3(f"/data/{ODD}", _registered(ODD), BUCKET) is None


def test_s3_does_not_search_by_subject() -> None:
    """Finding a dataset from a subject alone would mean guessing at names."""
    assert find_in_s3("841260", _registered(NAME, subject={"subject_id": "841260"}), BUCKET) is None
    assert find_in_s3("", _S3({}), BUCKET) is None


def test_resolution_prefers_the_registry() -> None:
    """DocDB first; S3 is not needed when it answers."""
    found = resolve_processed_dataset(
        NAME, [(V1, _Registry([_record(NAME)]))], _registered(NAME), BUCKET
    )
    assert found.source == "docdb_v1"


def test_resolution_falls_back_to_s3() -> None:
    """Unknown to the registry, but present in S3 under its exact name."""
    client = _registered(ODD, subject={"subject_id": "841260"})
    found = resolve_processed_dataset(ODD, [(V1, _Registry([]))], client, BUCKET)
    assert found.source == "s3"


def test_an_unknown_subject_is_refused_with_advice() -> None:
    """The error says what to pass instead."""
    with pytest.raises(DatasetNotFoundError, match="Pass the dataset name"):
        resolve_processed_dataset("841260", [(V1, _Registry([]))], _S3({}), BUCKET)


def test_an_unresolvable_name_is_an_error() -> None:
    """Neither source has it."""
    with pytest.raises(DatasetNotFoundError, match="does not resolve"):
        resolve_processed_dataset(ODD, [(V1, _Registry([]))], _S3({}), BUCKET)


def test_the_registry_is_consulted_v2_then_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    """The migrated index first, the original second."""
    from exaspim_swc_processing import sources

    built: list[str] = []
    monkeypatch.setattr(sources, "_docdb_client", lambda version, host: built.append(version))
    from exaspim_swc_processing.datasets import registry_sources

    assert [source for source, _ in registry_sources()] == [V2, V1]
    assert built == ["v2", "v1"]
