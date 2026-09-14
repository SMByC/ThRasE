from types import SimpleNamespace

import numpy as np
import pytest
from osgeo import gdal
from qgis.core import QgsVectorLayer

from ThRasE.core.editing import LayerToEdit, Pixel, PixelLog
from ThRasE.utils.qgis_utils import load_layer


@pytest.mark.parametrize("new_value", [1, 3])
def test_unrecorded_edit_reconciles_existing_record(editable_raster, new_value):
    editable_raster.data_provider.setEditable(True)
    pixel = Pixel(0.5, 3.5)
    editable_raster.edit_pixel(pixel, 2)
    editable_raster.registry.enabled = False
    editable_raster.edit_pixel(pixel, new_value)
    if new_value == 1:
        assert not editable_raster.pixel_log_store
    else:
        log = editable_raster.pixel_log_store[pixel]
        assert (log.old_value, log.new_value) == (1, 3)
    editable_raster.edit_pixel(Pixel(1.5, 3.5), 2)
    assert Pixel(1.5, 3.5) not in editable_raster.pixel_log_store


@pytest.mark.parametrize("value", [4294967295, (1 << 63) - 1, (1 << 64) - 1])
@pytest.mark.parametrize("extension", ["gpkg", "shp", "geojson"])
def test_registry_export_preserves_large_integer_values(editable_raster, tmp_path, value, extension):
    pixel = Pixel(0.5, 3.5)
    editable_raster.pixel_log_store[pixel] = PixelLog(pixel, 1, value, None, store=False)
    path = str(tmp_path / ("registry." + extension))
    ok, message, count = editable_raster.registry.export_registry(path)
    assert ok, message
    assert count == 1
    layer = QgsVectorLayer(path, "export", "ogr")
    assert layer.isValid()
    assert int(next(layer.getFeatures())["new_value"]) == value


@pytest.mark.parametrize("extension", ["gpkg", "shp", "geojson"])
def test_registry_export_keeps_numeric_fields_for_ordinary_values(editable_raster, tmp_path, extension):
    """The text fallback is only for values a numeric field would lose."""
    pixel = Pixel(0.5, 3.5)
    editable_raster.pixel_log_store[pixel] = PixelLog(pixel, 1, 2, None, store=False)
    path = str(tmp_path / ("registry." + extension))
    ok, message, _count = editable_raster.registry.export_registry(path)
    assert ok, message
    layer = QgsVectorLayer(path, "export", "ogr")
    assert layer.isValid()
    assert layer.fields().field("old_value").isNumeric()
    assert layer.fields().field("new_value").isNumeric()
    assert next(layer.getFeatures())["new_value"] == 2


def test_reenabling_registry_rebuilds_reconciled_groups(editable_raster, editing_ui):
    import uuid

    dialog, _view = editing_ui
    editable_raster.data_provider.setEditable(True)
    pixel = Pixel(0.5, 3.5)
    editable_raster.edit_pixel(pixel, 2, group_id=uuid.uuid4())
    editable_raster.registry.update()
    assert len(editable_raster.registry.groups) == 1
    dialog.registry_widget.toggle_registry_enabled(False)
    editable_raster.edit_pixel(pixel, 1)
    dialog.registry_widget.toggle_registry_enabled(True)
    assert not editable_raster.registry.groups


def test_registry_browser_clamps_saved_position_after_groups_change(editable_raster, editing_ui, monkeypatch):
    import uuid

    dialog, _view = editing_ui
    widget = dialog.registry_widget
    monkeypatch.setattr(widget, "isVisible", lambda: True)
    editable_raster.data_provider.setEditable(True)
    editable_raster.edit_pixel(Pixel(0.5, 3.5), 2, group_id=uuid.uuid4())
    widget.last_slider_position = 99
    widget.update_registry(go_to_last=False)
    assert widget.PixelLogGroups_Slider.value() == 1
    assert editable_raster.registry.current_group.idx == 1


@pytest.mark.usefixtures("qgis_new_project", "thrase_dialog")
def test_registry_groups_non_adjacent_logs_and_rebuilds_membership(tmp_path):
    source = tmp_path / "registry.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 3, 1, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, 1, 0, -1))
    dataset.GetRasterBand(1).WriteArray(np.array([[1, 1, 1]], dtype=np.uint8))
    dataset = None
    layer = load_layer(str(source), name="registry")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit

    PixelLog(Pixel(x=0.5, y=0.5), 1, 2, "first")
    PixelLog(Pixel(x=1.5, y=0.5), 1, 2, "second")
    PixelLog(Pixel(x=2.5, y=0.5), 1, 2, "first")
    assert layer_to_edit.registry.update(force_rebuild=True)

    groups = {group.group_id: group for group in layer_to_edit.registry.groups}
    assert len(groups["first"].tiles) == 2
    assert len(groups["second"].tiles) == 1
    layer_to_edit.registry.memory_layer.setSubsetString("")
    assert layer_to_edit.registry.memory_layer.featureCount() == 3
    layer_to_edit.registry.memory_layer.setSubsetString("FALSE")

    layer_to_edit.pixel_log_store[Pixel(x=1.5, y=0.5)].group_id = "first"
    assert layer_to_edit.registry.update()
    groups = {group.group_id: group for group in layer_to_edit.registry.groups}
    assert len(groups) == 1
    assert len(groups["first"].tiles) == 3
    layer_to_edit.registry.memory_layer.setSubsetString("")
    assert layer_to_edit.registry.memory_layer.featureCount() == 3


@pytest.mark.usefixtures("qgis_new_project", "thrase_dialog")
def test_failed_incremental_registry_insert_does_not_create_phantom_group(tmp_path, monkeypatch):
    source = tmp_path / "registry-failure.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 3, 1, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, 1, 0, -1))
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="registry failure")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit

    PixelLog(Pixel(x=0.5, y=0.5), 1, 2, "first")
    assert layer_to_edit.registry.update(force_rebuild=True)
    PixelLog(Pixel(x=1.5, y=0.5), 1, 2, "second")

    provider_class = type(layer_to_edit.registry.memory_layer.dataProvider())

    def fail_insert(_provider, _features, *_args, **_kwargs):
        return False, []

    monkeypatch.setattr(provider_class, "addFeatures", fail_insert)
    with pytest.raises(RuntimeError, match="replacement pixel registry"):
        layer_to_edit.registry.update()
    assert [group.group_id for group in layer_to_edit.registry.groups] == ["first"]

    monkeypatch.undo()
    assert layer_to_edit.registry.update()
    assert {group.group_id for group in layer_to_edit.registry.groups} == {"first", "second"}
    assert all(tile.feature_id is not None for group in layer_to_edit.registry.groups for tile in group.tiles)


@pytest.mark.usefixtures("qgis_new_project", "thrase_dialog")
def test_reconcile_registry_failure_preserves_all_log_state(tmp_path, monkeypatch):
    source = tmp_path / "registry-reconcile.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 2, 1, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, 1, 0, -1))
    dataset.GetRasterBand(1).Fill(3)
    dataset = None
    layer = load_layer(str(source), name="registry reconcile")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    PixelLog(Pixel(x=0.5, y=0.5), 1, 2, "first")
    PixelLog(Pixel(x=1.5, y=0.5), 1, 2, "first")
    previous_store = layer_to_edit.pixel_log_store
    previous_items = tuple(previous_store.items())
    calls = 0

    def fail_second_read(_point):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated provider read failure")
        return 3

    monkeypatch.setattr(layer_to_edit, "get_pixel_value_from_pnt", fail_second_read)
    with pytest.raises(RuntimeError, match="simulated provider read failure"):
        layer_to_edit.reconcile_registry()

    assert layer_to_edit.pixel_log_store is previous_store
    assert tuple(layer_to_edit.pixel_log_store.items()) == previous_items
    assert all(pixel_log.new_value == 2 for pixel_log in layer_to_edit.pixel_log_store.values())


@pytest.mark.usefixtures("qgis_new_project", "thrase_dialog")
def test_reconcile_registry_uses_worker_values_without_provider_queries(tmp_path, monkeypatch):
    source = tmp_path / "registry-worker-values.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 2, 1, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, 1, 0, -1))
    dataset.GetRasterBand(1).Fill(3)
    dataset = None
    layer = load_layer(str(source), name="registry worker values")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    first = Pixel(x=0.5, y=0.5)
    second = Pixel(x=1.5, y=0.5)
    PixelLog(first, 1, 2, "first")
    PixelLog(second, 1, 2, "first")

    monkeypatch.setattr(
        layer_to_edit,
        "get_pixel_value_from_pnt",
        lambda _point: (_ for _ in ()).throw(AssertionError("provider should not be queried")),
    )
    layer_to_edit.reconcile_registry((3, 1), (first, second))

    assert list(layer_to_edit.pixel_log_store) == [first]
    assert layer_to_edit.pixel_log_store[first].new_value == 3


@pytest.mark.usefixtures("qgis_new_project", "thrase_dialog")
def test_global_edit_registry_failure_preserves_store_and_visible_groups(tmp_path, monkeypatch):
    source = tmp_path / "registry-global-edit-failure.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 2, 1, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, 1, 0, -1))
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="registry global edit failure")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    PixelLog(Pixel(x=0.5, y=0.5), 1, 2, "first")
    assert layer_to_edit.registry.update(force_rebuild=True)
    previous_store = layer_to_edit.pixel_log_store

    provider_class = type(layer_to_edit.registry.memory_layer.dataProvider())
    monkeypatch.setattr(provider_class, "addFeatures", lambda *_args, **_kwargs: (False, []))
    change = SimpleNamespace(row=0, column=1, old_value=1, new_value=3)
    with pytest.raises(RuntimeError, match="replacement pixel registry"):
        layer_to_edit.store_global_edit_changes((change,), (0, 1, 0, 1, 0, -1))

    assert layer_to_edit.pixel_log_store is previous_store
    assert len(layer_to_edit.pixel_log_store) == 1
    assert len(layer_to_edit.registry.groups) == 1
    assert len(layer_to_edit.registry.groups[0].tiles) == 1


@pytest.mark.usefixtures("qgis_new_project", "thrase_dialog")
def test_global_edit_registry_canvas_failure_restores_store_and_registry_state(tmp_path, monkeypatch):
    source = tmp_path / "registry-canvas-failure.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 2, 1, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, 1, 0, -1))
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="registry canvas failure")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    PixelLog(Pixel(x=0.5, y=0.5), 1, 2, "first")
    assert layer_to_edit.registry.update(force_rebuild=True)
    previous_store = layer_to_edit.pixel_log_store
    previous_layer = layer_to_edit.registry.memory_layer
    previous_groups = layer_to_edit.registry.groups
    calls = 0

    def fail_first_canvas_update():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated canvas update failure")

    monkeypatch.setattr(layer_to_edit.registry, "update_registry_layer_in_canvases", fail_first_canvas_update)
    change = SimpleNamespace(row=0, column=1, old_value=1, new_value=3)
    with pytest.raises(RuntimeError, match="simulated canvas update failure"):
        layer_to_edit.store_global_edit_changes((change,), (0, 1, 0, 1, 0, -1))

    assert layer_to_edit.pixel_log_store is previous_store
    assert layer_to_edit.registry.memory_layer is previous_layer
    assert layer_to_edit.registry.groups is previous_groups
    assert len(layer_to_edit.registry.groups) == 1
    assert len(layer_to_edit.registry.groups[0].tiles) == 1


@pytest.mark.usefixtures("qgis_new_project", "thrase_dialog")
def test_new_group_is_appended_without_rebuilding_the_layer(tmp_path):
    source = tmp_path / "registry-append.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 3, 1, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, 1, 0, -1))
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="registry append")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit

    PixelLog(Pixel(x=0.5, y=0.5), 1, 2, "first")
    assert layer_to_edit.registry.update()
    first_layer = layer_to_edit.registry.memory_layer

    # One interactive edit adds one group: the existing layer is reused.
    PixelLog(Pixel(x=1.5, y=0.5), 1, 2, "second")
    assert layer_to_edit.registry.update()
    assert layer_to_edit.registry.memory_layer is first_layer
    assert [group.group_id for group in layer_to_edit.registry.groups] == ["first", "second"]
    assert [group.idx for group in layer_to_edit.registry.groups] == [1, 2]
    layer_to_edit.registry.memory_layer.setSubsetString("")
    assert layer_to_edit.registry.memory_layer.featureCount() == 2
    layer_to_edit.registry.memory_layer.setSubsetString("FALSE")

    # Losing a pixel from an existing group cannot be appended, so the layer is rebuilt.
    del layer_to_edit.pixel_log_store[Pixel(x=0.5, y=0.5)]
    assert layer_to_edit.registry.update()
    assert layer_to_edit.registry.memory_layer is not first_layer
    assert [group.group_id for group in layer_to_edit.registry.groups] == ["second"]
