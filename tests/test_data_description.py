"""Tests for :mod:`exaspim_swc_processing.data_description`."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aind_data_schema.core.data_description import DataDescription
from aind_data_schema_models.data_name_patterns import DataLevel

from exaspim_swc_processing.data_description import (
    DataDescriptionDerivationError,
    as_derived,
    derive_cell_data_description,
)
from exaspim_swc_processing.naming import parse_stem

RAW_ASSET = "exaSPIM_794492_2026-01-09_16-50-40"
PARENT_NAME = f"{RAW_ASSET}_processed_2026-01-17_14-44-16"
CREATION_TIME = datetime(2026, 8, 19, 22, 10, 2, tzinfo=timezone.utc)
RECONSTRUCTION = parse_stem("N001-794492-HP")


FIXTURE = Path(__file__).parent / "resources" / "parent_data_description.json"


def _parent(**overrides: object) -> DataDescription:
    """Build a parent data description from a real published exaSPIM asset.

    The fixture is the ``exaSPIM_794492`` processed asset's ``data_description``, upgraded
    from schema 1.0.4 to 2.4.1. It declares ``data_level: raw`` as exaSPIM assets do.

    Parameters
    ----------
    **overrides : object
        Fields to override on the fixture payload.

    Returns
    -------
    DataDescription
        The parent description.
    """
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(overrides)
    return DataDescription.model_validate(payload)


def test_derived_name_is_flat_not_accumulated() -> None:
    """The derived name builds on the raw asset, dropping the intermediate process."""
    derived = derive_cell_data_description(_parent(), RECONSTRUCTION, CREATION_TIME)
    assert derived.name == f"{RAW_ASSET}_reconstruction-N001_2026-08-19_22-10-02"
    assert "_processed_" not in derived.name


def test_source_data_records_the_immediate_parent() -> None:
    """Provenance points at the asset actually consumed, not the flattened raw name."""
    derived = derive_cell_data_description(_parent(), RECONSTRUCTION, CREATION_TIME)
    assert derived.source_data == [PARENT_NAME]


def test_derived_level_is_set() -> None:
    """The result is a derived asset regardless of what the parent claimed."""
    assert derive_cell_data_description(_parent(), RECONSTRUCTION, CREATION_TIME).data_level == (
        DataLevel.DERIVED
    )


def test_parent_is_not_mutated() -> None:
    """Coercing the level must not edit metadata we do not own."""
    parent = _parent()
    derive_cell_data_description(parent, RECONSTRUCTION, CREATION_TIME)
    assert parent.data_level == DataLevel.RAW


def test_administrative_fields_are_inherited() -> None:
    """Fields the parent carries flow through to the derived description."""
    derived = derive_cell_data_description(_parent(), RECONSTRUCTION, CREATION_TIME)
    assert derived.project_name == "MSMA Platform"
    assert derived.subject_id == "794492"
    assert [person.name for person in derived.investigators] == ["Jayaram Chandrashekar"]


def test_overrides_take_precedence_over_the_parent() -> None:
    """Explicit values win, so gaps in the parent can be filled by configuration."""
    derived = derive_cell_data_description(
        _parent(), RECONSTRUCTION, CREATION_TIME, project_name="Neuron Reconstruction"
    )
    assert derived.project_name == "Neuron Reconstruction"


def test_each_cell_gets_a_distinct_name() -> None:
    """Two cells in the same run differ by process label, not timestamp."""
    parent = _parent()
    first = derive_cell_data_description(parent, parse_stem("N001-794492-HP"), CREATION_TIME)
    second = derive_cell_data_description(parent, parse_stem("N003-794492-JG"), CREATION_TIME)
    assert first.name != second.name
    assert first.name.endswith("_reconstruction-N001_2026-08-19_22-10-02")
    assert second.name.endswith("_reconstruction-N003_2026-08-19_22-10-02")


def test_as_derived_passes_through_an_already_derived_parent() -> None:
    """A correctly declared parent is used as-is rather than copied."""
    parent = _parent(data_level="derived", source_data=[RAW_ASSET])
    assert as_derived(parent) is parent


def test_unparseable_parent_name_raises_a_clear_error() -> None:
    """A parent whose name is not in derived form cannot yield the raw input."""
    parent = _parent(name="not-an-aind-asset-name")
    with pytest.raises(DataDescriptionDerivationError, match="Could not derive"):
        derive_cell_data_description(parent, RECONSTRUCTION, CREATION_TIME)


def test_error_names_both_the_cell_and_the_parent() -> None:
    """The failure must identify which cell and which parent, for a 58-cell run."""
    parent = _parent(name="not-an-aind-asset-name")
    with pytest.raises(DataDescriptionDerivationError) as info:
        derive_cell_data_description(parent, RECONSTRUCTION, CREATION_TIME)
    assert "N001-794492-HP" in str(info.value)
    assert "not-an-aind-asset-name" in str(info.value)
