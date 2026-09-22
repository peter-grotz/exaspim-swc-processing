"""Tests for :mod:`exaspim_swc_processing.reference`."""

import numpy as np
import pytest

from exaspim_swc_processing.reference import (
    apply_geometry,
    reference_array,
    reference_arrays,
)
from exaspim_swc_processing.registration import (
    ANTS_DIRECTION,
    ORIGIN_MM,
    VolumeGeometry,
    loaded_geometry,
    parse_registration_record,
    resampled_geometry,
)
from tests.test_registration import _record

LOADED = VolumeGeometry((2061, 646, 1267), (0.010125, 0.010125, 0.0135))
RESAMPLED = VolumeGeometry((2087, 654, 1710), (0.01, 0.01, 0.01))


class _Image:
    """Records the geometry set on it, as ``ants.ANTsImage`` would store it."""

    def __init__(self) -> None:
        """Start with no geometry."""
        self.spacing: tuple[float, ...] | None = None
        self.origin: tuple[float, ...] | None = None
        self.direction: np.ndarray | None = None

    def set_spacing(self, spacing: tuple[float, float, float]) -> None:
        """Record the spacing.

        Parameters
        ----------
        spacing : tuple[float, float, float]
            Voxel size in millimetres.
        """
        self.spacing = spacing

    def set_origin(self, origin: tuple[float, float, float]) -> None:
        """Record the origin.

        Parameters
        ----------
        origin : tuple[float, float, float]
            Position of the first voxel.
        """
        self.origin = origin

    def set_direction(self, direction: np.ndarray) -> None:
        """Record the direction matrix.

        Parameters
        ----------
        direction : np.ndarray
            A 3x3 matrix of direction cosines.
        """
        self.direction = direction


def test_the_array_reports_the_geometry_shape() -> None:
    """``check_orientation`` mirrors about this shape, so it must be exact."""
    assert reference_array(LOADED).shape == (2061, 646, 1267)


def test_the_array_allocates_nothing() -> None:
    """A real 1.69 GB volume is replaced by a zero-strided view."""
    array = reference_array(LOADED)
    assert array.base is not None
    assert array.strides == (0, 0, 0)
    assert array.nbytes > array.base.nbytes


def test_the_array_is_read_only() -> None:
    """Nothing downstream writes to the reference volumes; enforce that."""
    with pytest.raises(ValueError, match="read-only"):
        reference_array(LOADED)[0, 0, 0] = 1


def test_both_arrays_are_built() -> None:
    """The transform needs a stand-in for each of the two volumes."""
    brain, resampled = reference_arrays(LOADED, RESAMPLED)
    assert brain.shape == (2061, 646, 1267)
    assert resampled.shape == (2087, 654, 1710)


def test_the_shapes_come_from_the_registration_record() -> None:
    """End to end: a real record and zarr shape give the verified 794492 geometry."""
    pass_ = parse_registration_record(_record())
    loaded = loaded_geometry(pass_, (646, 1267, 2061))
    brain, resampled = reference_arrays(loaded, resampled_geometry(loaded))
    assert brain.shape == (2061, 646, 1267)
    assert resampled.shape == (2087, 654, 1710)


def test_geometry_is_stamped_onto_the_image() -> None:
    """Spacing, origin and direction are what ``index_to_physical`` reads."""
    image = apply_geometry(_Image(), RESAMPLED)
    assert image.spacing == (0.01, 0.01, 0.01)
    assert image.origin == ORIGIN_MM
    assert np.array_equal(image.direction, np.array(ANTS_DIRECTION))


def test_the_direction_is_a_float_matrix() -> None:
    """ANTs rejects an integer direction matrix."""
    assert apply_geometry(_Image(), RESAMPLED).direction.dtype == np.dtype(float)


def test_index_to_physical_does_not_depend_on_shape() -> None:
    """The reason a 1x1x1 image is a valid stand-in: the conversion has no shape term.

    This reproduces ``CoordinateConverter.index_to_physical`` and shows that only the
    stamped geometry, never the image's extent, reaches the result.
    """

    def index_to_physical(image: _Image, index: np.ndarray) -> np.ndarray:
        direction = np.asarray(image.direction).reshape((3, 3))
        return np.asarray(image.origin) + (index * np.asarray(image.spacing)) @ direction.T

    points = np.arange(30, dtype=float).reshape(10, 3) * 37.0
    full = index_to_physical(apply_geometry(_Image(), RESAMPLED), points)
    minimal = index_to_physical(
        apply_geometry(_Image(), VolumeGeometry((1, 1, 1), RESAMPLED.spacing_mm)), points
    )
    assert np.array_equal(full, minimal)
