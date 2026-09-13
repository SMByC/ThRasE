import logging
import os
import re
from pathlib import Path

import numpy as np
import pytest
from osgeo import gdal, gdal_array, ogr, osr

from ThRasE.core.raster_recode import (
    GLOBAL_EDIT_LOGGER_NAME,
    RasterMaskSpec,
    RasterRecodeCancelled,
    RasterRecodeError,
    RasterRecodeRecoveryError,
    RecodePhase,
    RecodeRequest,
    RecodeStatus,
    VectorMaskSpec,
    commit_staged,
    count_recode_changes,
    discard_staged,
    finalize_commit,
    find_transaction_leftovers,
    rollback_commit,
    stage_recode,
)


def _projection(epsg=3116):
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromEPSG(epsg)
    return spatial_ref.ExportToWkt()


def _create_polygon_mask(path, *rings, epsg=3116):
    """Write a one-layer polygon GeoPackage, the on-disk form every vector mask takes."""
    driver = ogr.GetDriverByName("GPKG")
    dataset = driver.CreateDataSource(str(path))
    assert dataset is not None
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromEPSG(epsg)
    layer = dataset.CreateLayer("mask", spatial_ref, ogr.wkbPolygon)
    assert layer is not None
    for points in rings:
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for point in points:
            ring.AddPoint_2D(*point)
        polygon = ogr.Geometry(ogr.wkbPolygon)
        polygon.AddGeometry(ring)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(polygon)
        assert layer.CreateFeature(feature) == ogr.OGRERR_NONE
    layer = None
    dataset = None
    return path


def _create_raster(
    path,
    bands,
    data_type=gdal.GDT_Byte,
    geotransform=None,
    driver_name="GTiff",
    creation_options=None,
):
    arrays = [np.asarray(band) for band in bands]
    rows, columns = arrays[0].shape
    driver = gdal.GetDriverByName(driver_name)
    dataset = driver.Create(str(path), columns, rows, len(arrays), data_type, options=creation_options or [])
    assert dataset is not None
    dataset.SetGeoTransform(geotransform or (100, 10, 0, 200, 0, -10))
    dataset.SetProjection(_projection())
    dataset.SetMetadataItem("TEST_DATASET", "preserved")
    for index, array in enumerate(arrays, start=1):
        band = dataset.GetRasterBand(index)
        assert band.WriteArray(array) == gdal.CE_None
        band.SetMetadataItem("TEST_BAND", str(index))
    dataset.FlushCache()
    dataset = None
    return path


def _read(path, band=1):
    dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
    assert dataset is not None
    try:
        return dataset.GetRasterBand(band).ReadAsArray()
    finally:
        dataset = None


def _write(path, values, band=1):
    dataset = gdal.Open(str(path), gdal.GA_Update)
    assert dataset is not None
    assert dataset.GetRasterBand(band).WriteArray(np.asarray(values)) == gdal.CE_None
    dataset = None
    _mark_modified(path)


def _mark_modified(path):
    """Advance the modification time so stamp-based change detection sees an external write."""
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def _inode(path):
    return os.stat(path).st_ino


def _commit(result):
    receipt = commit_staged(result)
    assert finalize_commit(receipt) == ()


def _artifacts(path: Path):
    return list(path.parent.glob(f".{path.name}.thrase*"))


def _remove_directory(directory: Path):
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()


# --------------------------------------------------------------------------------------
# Recoding behaviour


def test_recode_is_non_cascading_and_uses_bounded_reads(tmp_path, monkeypatch):
    source = _create_raster(
        tmp_path / "source.tif",
        [np.array([[1, 2, 3, 4, 1, 2, 3], [3, 2, 1, 4, 3, 2, 1]] * 3, dtype=np.uint8)],
    )
    original_read = gdal_array.BandReadAsArray
    read_sizes = []

    def bounded_read(*args, **kwargs):
        xsize = kwargs.get("win_xsize", args[3] if len(args) > 3 else None)
        ysize = kwargs.get("win_ysize", args[4] if len(args) > 4 else None)
        assert xsize is not None and ysize is not None
        assert xsize <= 4 and ysize <= 3
        read_sizes.append((xsize, ysize))
        return original_read(*args, **kwargs)

    monkeypatch.setattr(gdal_array, "BandReadAsArray", bounded_read)
    result = stage_recode(
        RecodeRequest(
            str(source),
            1,
            ((1, 2), (2, 3), (3, 1)),
            window_width=4,
            window_height=3,
            memory_budget_bytes=1024,
        )
    )
    assert result.status is RecodeStatus.STAGED
    assert read_sizes
    _commit(result)
    monkeypatch.setattr(gdal_array, "BandReadAsArray", original_read)

    expected_row = np.array([2, 3, 1, 4, 2, 3, 1], dtype=np.uint8)
    np.testing.assert_array_equal(_read(source), np.vstack([expected_row, expected_row[::-1]] * 3))
    assert not _artifacts(source)


def test_stage_does_not_modify_source_and_no_change_leaves_nothing_behind(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2], [3, 4]], dtype=np.uint8)])
    before = source.read_bytes()
    result = stage_recode(RecodeRequest(str(source), 1, ((9, 10),)))
    assert result.status is RecodeStatus.NO_CHANGES
    assert result.stage_path is None
    assert result.lock_path is None
    assert source.read_bytes() == before
    assert not _artifacts(source)


def test_raster_mask_supports_integral_offset_and_partial_overlap(tmp_path):
    source_values = np.ones((4, 5), dtype=np.uint8)
    source = _create_raster(tmp_path / "source.tif", [source_values])
    mask = _create_raster(
        tmp_path / "mask.tif",
        [np.array([[7, 0, 7], [0, 7, 0]], dtype=np.uint8)],
        geotransform=(110, 10, 0, 190, 0, -10),
    )
    result = stage_recode(
        RecodeRequest(
            str(source),
            1,
            ((1, 9),),
            mask=RasterMaskSpec(str(mask), 1, (7,)),
            window_width=2,
            window_height=2,
            memory_budget_bytes=1024,
        )
    )
    assert result.changed_count == 3
    _commit(result)
    expected = source_values.copy()
    expected[1, 1] = 9
    expected[1, 3] = 9
    expected[2, 2] = 9
    np.testing.assert_array_equal(_read(source), expected)


def test_raster_mask_excludes_invalid_pixels(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((1, 2), dtype=np.uint8)])
    mask = _create_raster(tmp_path / "mask.tif", [np.full((1, 2), 7, dtype=np.uint8)])
    with gdal.config_option("GDAL_TIFF_INTERNAL_MASK", "YES"):
        dataset = gdal.Open(str(mask), gdal.GA_Update)
        band = dataset.GetRasterBand(1)
        assert band.CreateMaskBand(gdal.GMF_PER_DATASET) == gdal.CE_None
        assert band.GetMaskBand().WriteArray(np.array([[0, 255]], dtype=np.uint8)) == gdal.CE_None
        dataset = None

    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),), mask=RasterMaskSpec(str(mask), 1, (7,))))
    assert result.changed_count == 1
    _commit(result)
    np.testing.assert_array_equal(_read(source), [[1, 9]])


def test_vector_mask_is_rasterized_per_window(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((4, 5), dtype=np.uint8)])
    mask = _create_polygon_mask(tmp_path / "mask.gpkg", ((100, 200), (130, 200), (130, 180), (100, 180), (100, 200)))
    result = stage_recode(
        RecodeRequest(
            str(source),
            1,
            ((1, 5),),
            mask=VectorMaskSpec(source_path=str(mask), layer_name="mask", crs_wkt=_projection()),
            window_width=2,
            window_height=2,
            memory_budget_bytes=1024,
        )
    )
    assert result.changed_count == 6
    _commit(result)
    expected = np.ones((4, 5), dtype=np.uint8)
    expected[:2, :3] = 5
    np.testing.assert_array_equal(_read(source), expected)


def test_every_changed_pixel_is_collected_for_the_registry(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((3, 3), dtype=np.uint8)])
    # window_width=2 splits the raster into several windows, so the collected
    # changes are concatenated from more than one verification window.
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 2),), collect_changes=True, window_width=2))
    try:
        assert result.changed_count == 9
        assert result.changes is not None and len(result.changes) == 9
        assert {(change.row, change.column, change.old_value, change.new_value) for change in result.changes} == {
            (row, column, 1, 2) for row in range(3) for column in range(3)
        }
    finally:
        discard_staged(result)
    assert not _artifacts(source)


def test_changes_are_only_collected_when_requested(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((1, 3), dtype=np.uint8)])
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 2),)))
    try:
        assert result.changed_count == 3
        assert result.changes is None
    finally:
        discard_staged(result)


def test_collected_changes_are_empty_when_nothing_matched(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((1, 3), dtype=np.uint8)])
    result = stage_recode(RecodeRequest(str(source), 1, ((9, 2),), collect_changes=True))
    assert result.status is RecodeStatus.NO_CHANGES
    assert result.changes is not None and len(result.changes) == 0
    assert list(result.changes) == []
    assert not _artifacts(source)


def test_registry_values_are_read_from_verified_stage_windows(tmp_path):
    source = _create_raster(
        tmp_path / "source.tif",
        [np.array([[1, 2], [3, 1]], dtype=np.uint8)],
        geotransform=(100, 10, 0, 200, 0, -10),
    )
    result = stage_recode(
        RecodeRequest(
            str(source),
            1,
            ((1, 9),),
            registry_points=((105, 195), (115, 185), (500, 500)),
            window_width=1,
            window_height=1,
        )
    )
    try:
        assert result.registry_values == (9, 9, None)
    finally:
        discard_staged(result)


def test_cancellation_removes_stage_and_preserves_source(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((6, 6), dtype=np.uint8)])
    before = source.read_bytes()
    checks = 0

    def cancel_during_windows():
        nonlocal checks
        checks += 1
        return checks > 4

    with pytest.raises(RasterRecodeCancelled):
        stage_recode(
            RecodeRequest(str(source), 1, ((1, 2),), window_width=2, window_height=2),
            is_cancelled=cancel_during_windows,
        )
    assert source.read_bytes() == before
    assert not _artifacts(source)


def test_multiband_metadata_and_native_dtype_are_preserved(tmp_path):
    first = np.array([[1, 2], [3, 4]], dtype=np.uint16)
    second = np.array([[1000, 2000], [3000, 4000]], dtype=np.uint16)
    source = _create_raster(tmp_path / "source.tif", [first, second], data_type=gdal.GDT_UInt16)
    result = stage_recode(RecodeRequest(str(source), 2, ((2000, 60000),)))
    _commit(result)

    dataset = gdal.Open(str(source), gdal.GA_ReadOnly)
    assert dataset.RasterCount == 2
    assert dataset.GetMetadataItem("TEST_DATASET") == "preserved"
    assert dataset.GetRasterBand(2).DataType == gdal.GDT_UInt16
    assert dataset.GetRasterBand(2).GetMetadataItem("TEST_BAND") == "2"
    np.testing.assert_array_equal(dataset.GetRasterBand(1).ReadAsArray(), first)
    np.testing.assert_array_equal(dataset.GetRasterBand(2).ReadAsArray(), [[1000, 60000], [3000, 4000]])
    dataset = None


def test_out_of_range_value_is_rejected_before_staging(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((2, 2), dtype=np.uint8)])
    before = source.read_bytes()
    with pytest.raises(RasterRecodeError, match="outside the uint8 range"):
        stage_recode(RecodeRequest(str(source), 1, ((1, 256),)))
    assert source.read_bytes() == before
    assert not _artifacts(source)


def test_packed_integer_range_is_enforced_before_staging(tmp_path):
    source = _create_raster(
        tmp_path / "packed.tif",
        [np.array([[0, 1]], dtype=np.uint8)],
        creation_options=["NBITS=1"],
    )
    with pytest.raises(RasterRecodeError, match="outside the NBITS=1 uint8 range"):
        stage_recode(RecodeRequest(str(source), 1, ((1, 2),)))
    np.testing.assert_array_equal(_read(source), [[0, 1]])
    assert not _artifacts(source)


def test_representable_packed_integer_recode_is_verified(tmp_path):
    source = _create_raster(
        tmp_path / "packed.tif",
        [np.array([[0, 1]], dtype=np.uint8)],
        creation_options=["NBITS=1"],
    )
    result = stage_recode(RecodeRequest(str(source), 1, ((0, 1),)))
    _commit(result)
    np.testing.assert_array_equal(_read(source), [[1, 1]])


def test_active_nodata_source_value_is_rejected(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[0, 1]], dtype=np.uint8)])
    dataset = gdal.Open(str(source), gdal.GA_Update)
    dataset.GetRasterBand(1).SetNoDataValue(0)
    dataset = None
    with pytest.raises(RasterRecodeError, match="NoData value 0 cannot be recoded"):
        stage_recode(RecodeRequest(str(source), 1, ((0, 2),)))


def test_misaligned_raster_mask_is_rejected(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((2, 2), dtype=np.uint8)])
    mask = _create_raster(
        tmp_path / "mask.tif",
        [np.ones((2, 2), dtype=np.uint8)],
        geotransform=(105, 10, 0, 200, 0, -10),
    )
    with pytest.raises(RasterRecodeError, match="origin is not aligned"):
        stage_recode(RecodeRequest(str(source), 1, ((1, 2),), mask=RasterMaskSpec(str(mask), 1, (1,))))


def test_raster_mask_change_during_staging_is_rejected(tmp_path, monkeypatch):
    from ThRasE.core import raster_recode

    source = _create_raster(tmp_path / "source.tif", [np.ones((2, 2), dtype=np.uint8)])
    mask = _create_raster(tmp_path / "mask.tif", [np.ones((2, 2), dtype=np.uint8)])
    original_selection = raster_recode._RasterMaskReader.selection
    changed = False

    def change_mask_after_read(reader, *args):
        nonlocal changed
        selected = original_selection(reader, *args)
        if not changed:
            changed = True
            _write(mask, np.full((2, 2), 2, dtype=np.uint8))
        return selected

    monkeypatch.setattr(raster_recode._RasterMaskReader, "selection", change_mask_after_read)
    with pytest.raises(RasterRecodeError, match="raster mask changed"):
        stage_recode(
            RecodeRequest(
                str(source),
                1,
                ((1, 9),),
                mask=RasterMaskSpec(str(mask), 1, (1,)),
                window_width=1,
                window_height=1,
            )
        )
    np.testing.assert_array_equal(_read(source), np.ones((2, 2), dtype=np.uint8))
    assert not _artifacts(source)


def test_raster_mask_change_is_rejected_on_no_changes_path(tmp_path, monkeypatch):
    from ThRasE.core import raster_recode

    source = _create_raster(tmp_path / "source.tif", [np.ones((1, 1), dtype=np.uint8)])
    mask = _create_raster(tmp_path / "mask.tif", [np.full((1, 1), 2, dtype=np.uint8)])
    original_selection = raster_recode._RasterMaskReader.selection
    changed = False

    def change_mask_before_read(reader, *args):
        nonlocal changed
        if not changed:
            changed = True
            _write(mask, np.full((1, 1), 1, dtype=np.uint8))
        return original_selection(reader, *args)

    monkeypatch.setattr(raster_recode._RasterMaskReader, "selection", change_mask_before_read)
    with pytest.raises(RasterRecodeError, match="raster mask changed"):
        stage_recode(RecodeRequest(str(source), 1, ((1, 9),), mask=RasterMaskSpec(str(mask), 1, (1,))))
    np.testing.assert_array_equal(_read(source), [[1]])
    assert not _artifacts(source)


def test_unsupported_driver_is_rejected(tmp_path):
    backing = _create_raster(tmp_path / "backing.tif", [np.ones((2, 2), dtype=np.uint8)])
    vrt = tmp_path / "source.vrt"
    dataset = gdal.Translate(str(vrt), str(backing), format="VRT")
    assert dataset is not None
    dataset = None
    with pytest.raises(RasterRecodeError, match="does not support the VRT driver"):
        stage_recode(RecodeRequest(str(vrt), 1, ((1, 2),)))


def test_hfa_raster_with_attribute_table_is_edited_and_keeps_classes(tmp_path):
    if gdal.GetDriverByName("HFA") is None:
        pytest.skip("HFA driver is unavailable")
    source = _create_raster(tmp_path / "source.img", [np.array([[1, 2], [3, 1]], dtype=np.uint8)], driver_name="HFA")
    dataset = gdal.Open(str(source), gdal.GA_Update)
    attribute_table = gdal.RasterAttributeTable()
    attribute_table.CreateColumn("Value", gdal.GFT_Integer, gdal.GFU_MinMax)
    attribute_table.CreateColumn("Class_Names", gdal.GFT_String, gdal.GFU_Name)
    attribute_table.SetRowCount(2)
    attribute_table.SetValueAsInt(0, 0, 1)
    attribute_table.SetValueAsString(0, 1, "forest")
    attribute_table.SetValueAsInt(1, 0, 2)
    attribute_table.SetValueAsString(1, 1, "water")
    assert dataset.GetRasterBand(1).SetDefaultRAT(attribute_table) == gdal.CE_None
    dataset = None

    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    _commit(result)
    np.testing.assert_array_equal(_read(source), [[9, 2], [3, 9]])
    dataset = gdal.Open(str(source), gdal.GA_ReadOnly)
    restored = dataset.GetRasterBand(1).GetDefaultRAT()
    assert restored is not None and restored.GetRowCount() == 2
    column_names = [restored.GetNameOfCol(index) for index in range(restored.GetColumnCount())]
    assert restored.GetValueAsString(1, column_names.index("Class_Names")) == "water"
    dataset = None
    assert not _artifacts(source)


def test_class_metadata_is_kept_and_pixel_counts_are_dropped(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 1]], dtype=np.uint8)])
    dataset = gdal.Open(str(source), gdal.GA_Update)
    band = dataset.GetRasterBand(1)
    attribute_table = gdal.RasterAttributeTable()
    attribute_table.CreateColumn("Value", gdal.GFT_Integer, gdal.GFU_MinMax)
    attribute_table.CreateColumn("Count", gdal.GFT_Integer, gdal.GFU_PixelCount)
    attribute_table.SetRowCount(1)
    attribute_table.SetValueAsInt(0, 0, 1)
    attribute_table.SetValueAsInt(0, 1, 2)
    assert band.SetDefaultRAT(attribute_table) == gdal.CE_None
    color_table = gdal.ColorTable()
    color_table.SetColorEntry(1, (255, 0, 0, 255))
    assert band.SetColorTable(color_table) == gdal.CE_None
    dataset = None

    result = stage_recode(RecodeRequest(str(source), 1, ((1, 2),)))
    _commit(result)
    np.testing.assert_array_equal(_read(source), [[2, 2]])
    dataset = gdal.Open(str(source), gdal.GA_ReadOnly)
    band = dataset.GetRasterBand(1)
    restored = band.GetDefaultRAT()
    assert restored is not None and restored.GetRowCount() == 1
    assert [restored.GetNameOfCol(index) for index in range(restored.GetColumnCount())] == ["Value"]
    assert band.GetColorTable() is not None
    assert band.GetColorTable().GetColorEntry(1) == (255, 0, 0, 255)
    assert band.GetColorInterpretation() == gdal.GCI_PaletteIndex
    dataset = None


def test_stale_statistics_and_overviews_are_removed(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((16, 16), dtype=np.uint8)])
    dataset = gdal.Open(str(source), gdal.GA_Update)
    dataset.GetRasterBand(1).ComputeStatistics(False)
    assert dataset.BuildOverviews("NEAREST", [2]) == gdal.CE_None
    dataset = None
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 2),)))
    _commit(result)

    dataset = gdal.Open(str(source), gdal.GA_ReadOnly)
    band = dataset.GetRasterBand(1)
    assert band.GetOverviewCount() == 0
    assert band.GetMetadataItem("STATISTICS_MINIMUM") is None
    dataset = None
    assert not _artifacts(source)


def test_deferred_stage_flush_failure_preserves_source(tmp_path, monkeypatch):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    original_flush = gdal.Dataset.FlushCache

    def fail_stage_flush(dataset, *args, **kwargs):
        if ".thrase-stage-" in dataset.GetDescription():
            return gdal.CE_Failure
        return original_flush(dataset, *args, **kwargs)

    monkeypatch.setattr(gdal.Dataset, "FlushCache", fail_stage_flush)
    with pytest.raises(RasterRecodeError, match="write the staging raster"):
        stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    np.testing.assert_array_equal(_read(source), [[1, 2]])
    assert not _artifacts(source)


def test_staged_value_readback_rejects_silent_write_loss(tmp_path, monkeypatch):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    original_write = gdal_array.BandWriteArray

    def ignore_stage_write(band, *args, **kwargs):
        if ".thrase-stage-" in band.GetDataset().GetDescription():
            return gdal.CE_None
        return original_write(band, *args, **kwargs)

    monkeypatch.setattr(gdal_array, "BandWriteArray", ignore_stage_write)
    with pytest.raises(RasterRecodeError, match="does not hold the recoded values"):
        stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    np.testing.assert_array_equal(_read(source), [[1, 2]])
    assert not _artifacts(source)


def test_numpy_working_allocations_stay_within_memory_budget(tmp_path):
    import tracemalloc

    source = _create_raster(tmp_path / "source.tif", [np.ones((2048, 2048), dtype=np.uint8)])
    memory_budget = 1024 * 1024
    tracemalloc.start()
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 2),), memory_budget_bytes=memory_budget))
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    try:
        assert peak <= memory_budget * 1.25
    finally:
        discard_staged(result)


def test_stage_temporarily_bounds_gdal_block_cache(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((64, 64), dtype=np.uint8)])
    memory_budget = 1024 * 1024
    previous_limit = gdal.GetCacheMax()
    observed_limits = []

    result = stage_recode(
        RecodeRequest(str(source), 1, ((1, 2),), memory_budget_bytes=memory_budget),
        progress_callback=lambda _value: observed_limits.append(gdal.GetCacheMax()),
    )
    try:
        assert observed_limits
        assert max(observed_limits) <= memory_budget // 4
        assert gdal.GetCacheMax() == previous_limit
    finally:
        discard_staged(result)


def test_staging_directory_is_private_and_copy_keeps_source_permissions(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    source.chmod(0o600)
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    try:
        assert result.stage_path is not None
        stage = Path(result.stage_path)
        assert stage.parent.stat().st_mode & 0o777 == 0o700
        assert stage.stat().st_mode & 0o777 == 0o600
    finally:
        discard_staged(result)
    assert not _artifacts(source)


# --------------------------------------------------------------------------------------
# Commit, rollback, finalization, and recovery


def test_commit_swaps_files_by_rename_and_finalize_removes_backup(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    original_inode = _inode(source)
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    assert result.status is RecodeStatus.STAGED and result.stage_path is not None
    stage_inode = _inode(result.stage_path)

    receipt = commit_staged(result)
    assert _inode(source) == stage_inode, "the staged file must be renamed into place, not copied"
    assert _inode(receipt.backup_path) == original_inode, "the original must be renamed into the backup directory"
    np.testing.assert_array_equal(_read(source), [[9, 2]])
    np.testing.assert_array_equal(_read(receipt.backup_path), [[1, 2]])
    assert find_transaction_leftovers(str(source))

    assert finalize_commit(receipt) == ()
    assert not _artifacts(source)
    assert find_transaction_leftovers(str(source)) == ()


def test_commit_preserves_source_file_permissions(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    source.chmod(0o640)
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    _commit(result)

    assert source.stat().st_mode & 0o777 == 0o640
    np.testing.assert_array_equal(_read(source), [[9, 2]])


def test_explicit_rollback_restores_original_dataset(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    original_inode = _inode(source)
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    receipt = commit_staged(result)
    np.testing.assert_array_equal(_read(source), [[9, 2]])

    rollback_commit(receipt)
    np.testing.assert_array_equal(_read(source), [[1, 2]])
    assert _inode(source) == original_inode
    assert finalize_commit(receipt) == ()
    assert not _artifacts(source)


def test_commit_rejects_source_changed_after_staging(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    _write(source, [[3, 2]])
    try:
        with pytest.raises(RasterRecodeError, match="changed after the edit was prepared"):
            commit_staged(result)
    finally:
        discard_staged(result)
    np.testing.assert_array_equal(_read(source), [[3, 2]])
    assert not _artifacts(source)


def test_commit_refuses_when_source_is_missing(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    gdal.GetDriverByName("GTiff").Delete(str(source))
    try:
        with pytest.raises(RasterRecodeError, match="changed after the edit was prepared"):
            commit_staged(result)
        assert not list(tmp_path.glob(".source.tif.thrase-backup-*"))
    finally:
        discard_staged(result)
    assert not _artifacts(source)


def test_commit_rejects_stage_tampering_after_verification(tmp_path):
    source = _create_raster(
        tmp_path / "source.tif",
        [np.array([[1, 2]], dtype=np.uint8), np.array([[4, 5]], dtype=np.uint8)],
    )
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    assert result.stage_path is not None
    _write(result.stage_path, [[77, 77]], band=2)
    try:
        with pytest.raises(RasterRecodeError, match="staged raster changed"):
            commit_staged(result)
    finally:
        discard_staged(result)
    np.testing.assert_array_equal(_read(source, 1), [[1, 2]])
    np.testing.assert_array_equal(_read(source, 2), [[4, 5]])
    assert not _artifacts(source)


def test_commit_failure_midway_leaves_source_unchanged(tmp_path, monkeypatch):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    original_inode = _inode(source)
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    assert result.stage_path is not None
    original_replace = os.replace

    def fail_installing_stage(rename_source, destination):
        if os.fspath(rename_source) == result.stage_path:
            raise PermissionError("simulated rename failure")
        return original_replace(rename_source, destination)

    monkeypatch.setattr(os, "replace", fail_installing_stage)
    with pytest.raises(RasterRecodeError, match="left unchanged"):
        commit_staged(result)
    monkeypatch.undo()

    assert _inode(source) == original_inode
    np.testing.assert_array_equal(_read(source), [[1, 2]])
    assert not list(tmp_path.glob(".source.tif.thrase-backup-*"))
    assert Path(result.stage_path).exists()
    assert discard_staged(result) == ()
    assert not _artifacts(source)


def test_rollback_rejects_backup_changed_after_commit(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    receipt = commit_staged(result)
    _write(receipt.backup_path, [[77, 2]])

    with pytest.raises(RasterRecodeRecoveryError, match="changed after the commit"):
        rollback_commit(receipt)
    np.testing.assert_array_equal(_read(source), [[9, 2]])
    assert finalize_commit(receipt) == (receipt.backup_path,)
    _remove_directory(Path(receipt.backup_path).parent)
    assert not _artifacts(source)


def test_finalize_keeps_backup_written_after_commit(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    receipt = commit_staged(result)
    # A program that still had the old file open writes through its handle after the swap.
    _write(receipt.backup_path, [[4, 2]])

    retained = finalize_commit(receipt)
    assert retained == (receipt.backup_path,)
    np.testing.assert_array_equal(_read(source), [[9, 2]])
    np.testing.assert_array_equal(_read(receipt.backup_path), [[4, 2]])
    backup_directory = Path(receipt.backup_path).parent
    assert find_transaction_leftovers(str(source)) == (str(backup_directory),)
    with pytest.raises(RasterRecodeRecoveryError, match="unfinished ThRasE edit"):
        stage_recode(RecodeRequest(str(source), 1, ((9, 1),)))

    _remove_directory(backup_directory)
    assert not _artifacts(source)


def test_lock_prevents_concurrent_edits(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    first = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    try:
        assert first.lock_path is not None and Path(first.lock_path).exists()
        with pytest.raises(RasterRecodeRecoveryError, match="unfinished ThRasE edit"):
            stage_recode(RecodeRequest(str(source), 1, ((1, 8),)))
    finally:
        discard_staged(first)
    assert not _artifacts(source)


def test_leftover_transaction_files_block_edits_until_removed(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    leftover = tmp_path / ".source.tif.thrase-backup-deadbeef"
    leftover.mkdir()
    assert find_transaction_leftovers(str(source)) == (str(leftover),)
    with pytest.raises(RasterRecodeRecoveryError, match=re.escape(str(leftover))):
        stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    np.testing.assert_array_equal(_read(source), [[1, 2]])

    leftover.rmdir()
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    _commit(result)
    np.testing.assert_array_equal(_read(source), [[9, 2]])
    assert not _artifacts(source)


def test_no_change_reports_lock_removal_failure(tmp_path, monkeypatch, caplog):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    lock_path = tmp_path / ".source.tif.thrase.lock"
    original_remove = os.remove

    def fail_lock_removal(path):
        if os.fspath(path) == str(lock_path):
            raise PermissionError("simulated lock removal failure")
        return original_remove(path)

    monkeypatch.setattr(os, "remove", fail_lock_removal)
    with (
        caplog.at_level(logging.WARNING, logger=GLOBAL_EDIT_LOGGER_NAME),
        pytest.raises(RasterRecodeError, match="raster lock"),
    ):
        stage_recode(RecodeRequest(str(source), 1, ((9, 2),)))
    monkeypatch.undo()
    assert any("raster lock" in record.getMessage() for record in caplog.records)

    assert lock_path.exists()
    lock_path.unlink()
    assert not _artifacts(source)


def test_discard_keeps_files_for_recovery_when_requested(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    assert result.stage_path is not None and result.lock_path is not None

    kept = discard_staged(result, keep_for_recovery=True)
    assert set(kept) == {str(Path(result.stage_path).parent), result.lock_path}
    assert all(Path(path).exists() for path in kept)
    with pytest.raises(RasterRecodeRecoveryError, match="unfinished ThRasE edit"):
        stage_recode(RecodeRequest(str(source), 1, ((1, 8),)))

    assert discard_staged(result) == ()
    assert not _artifacts(source)


def test_discard_reports_files_it_could_not_remove(tmp_path, monkeypatch, caplog):
    from ThRasE.core import raster_recode

    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)))
    assert result.stage_path is not None and result.lock_path is not None

    def fail_removal(_directory):
        raise RasterRecodeError("simulated cleanup failure")

    monkeypatch.setattr(raster_recode, "_remove_transaction_directory", fail_removal)
    with caplog.at_level(logging.WARNING, logger=GLOBAL_EDIT_LOGGER_NAME):
        leftovers = discard_staged(result)
    assert any("simulated cleanup failure" in record.getMessage() for record in caplog.records)
    assert leftovers == (str(Path(result.stage_path).parent),)
    assert not Path(result.lock_path).exists()

    monkeypatch.undo()
    assert discard_staged(result) == ()
    assert not _artifacts(source)


def test_staging_reports_its_steps_in_order(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2]], dtype=np.uint8)])
    phases = []
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 9),)), phase_callback=phases.append)
    try:
        assert phases == [RecodePhase.COPYING, RecodePhase.RECODING, RecodePhase.VERIFYING]
    finally:
        discard_staged(result)

    # nothing to verify when nothing changed
    phases.clear()
    result = stage_recode(RecodeRequest(str(source), 1, ((7, 9),)), phase_callback=phases.append)
    assert result.status is RecodeStatus.NO_CHANGES
    assert phases == [RecodePhase.COPYING, RecodePhase.RECODING]
    assert not _artifacts(source)


def test_counting_changes_matches_staging_and_writes_nothing(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.array([[1, 2, 1], [3, 1, 2]], dtype=np.uint8)])
    mask = _create_raster(tmp_path / "mask.tif", [np.array([[7, 7, 0], [7, 0, 7]], dtype=np.uint8)])
    request = RecodeRequest(
        str(source),
        1,
        ((1, 9), (2, 8)),
        mask=RasterMaskSpec(str(mask), 1, (7,)),
        window_width=2,
        window_height=1,
        memory_budget_bytes=1024,
    )
    progress = []

    assert count_recode_changes(request, progress_callback=progress.append) == 3
    assert progress and progress[-1] == 100.0
    assert not _artifacts(source)
    np.testing.assert_array_equal(_read(source), [[1, 2, 1], [3, 1, 2]])

    result = stage_recode(request)
    try:
        assert result.changed_count == 3
    finally:
        discard_staged(result)


def test_counting_changes_can_be_cancelled(tmp_path):
    source = _create_raster(tmp_path / "source.tif", [np.ones((4, 4), dtype=np.uint8)])
    with pytest.raises(RasterRecodeCancelled):
        count_recode_changes(RecodeRequest(str(source), 1, ((1, 2),), window_width=2), is_cancelled=lambda: True)
    assert not _artifacts(source)
