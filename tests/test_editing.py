"""
/***************************************************************************
 ThRasE

 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2026 by Xavier Corredor Llano, SMByC
        email                : xavier.corredor.llano@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

from types import SimpleNamespace

import numpy as np
import pytest
from osgeo import gdal
from qgis.core import QgsGeometry, QgsPointXY, QgsRasterLayer, QgsRectangle
from qgis.PyQt.QtCore import Qt

from ThRasE.core.editing import EditLog, LayerToEdit, Pixel, edit_layer
from ThRasE.gui.apply_from_classes_or_mask import ApplyFromClassesOrMask
from ThRasE.utils.qgis_utils import get_pixel_centroid, load_layer


def test_pixel_keys_compare_coordinates_and_stay_stable(monkeypatch):
    monkeypatch.setattr(LayerToEdit, "current", SimpleNamespace(pixel_tolerance=2))
    first, second = Pixel(-1, 5), Pixel(-2, 5)
    assert first != second
    pixels = {Pixel(1.234, 5): "saved"}
    key = next(iter(pixels))
    monkeypatch.setattr(LayerToEdit, "current", SimpleNamespace(pixel_tolerance=0))
    assert pixels[key] == "saved"


def test_history_is_not_available_on_another_target(monkeypatch):
    first = SimpleNamespace(pixel_tolerance=2)
    monkeypatch.setattr(LayerToEdit, "current", first)
    history = EditLog("pixel")
    history.add((Pixel(1, 1), 7))
    monkeypatch.setattr(LayerToEdit, "current", SimpleNamespace(pixel_tolerance=2))
    assert not history.can_be_undone()
    assert history.undo() is None


@pytest.mark.parametrize("initially_editable", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_editable_state_restored(editable_raster, initially_editable, fail):
    provider = editable_raster.data_provider
    provider.setEditable(initially_editable)

    @edit_layer
    def operation():
        assert provider.isEditable()
        if fail:
            raise ValueError("injected failure")
        return "success"

    if fail:
        with pytest.raises(ValueError, match="injected failure"):
            operation()
    else:
        assert operation() == "success"
    assert provider.isEditable() == initially_editable


@pytest.mark.parametrize("value", [-1, 300])
def test_manual_edit_rejects_overflow(editable_raster, value):
    editable_raster.data_provider.setEditable(True)
    with pytest.raises(ValueError):
        editable_raster.edit_pixel(Pixel(0.5, 3.5), value)
    assert editable_raster.get_pixel_value_from_xy(0.5, 3.5) == 1


@pytest.mark.parametrize("x, y", [(-0.1, 4.1), (4, 2), (2, 0), (-0.1, 2), (2, 4.1)])
def test_outside_point_does_not_snap_inside(editable_raster, x, y):
    assert get_pixel_centroid(x, y) is None
    assert editable_raster.edit_pixel(Pixel(x, y), 2) is None
    assert not editable_raster.check_point_inside_layer(QgsPointXY(x, y))


@pytest.mark.parametrize("text, expected", [("1", None), ("nonsense", 7), ("300", 7), ("", None)])
def test_recode_input_keeps_model_and_mapping_consistent(editable_raster, editing_ui, text, expected):
    dialog, _view = editing_ui
    dialog.recodePixelTable.item(0, 3).setText(text)
    dialog.update_recode_pixel_table()
    assert editable_raster.pixels[0]["new_value"] == expected
    assert editable_raster.old_new_value == ({} if expected is None else {1: expected})


@pytest.mark.parametrize("restored_value", [300, -1, 2.5])
def test_unwritable_restored_mapping_is_cleared(editable_raster, editing_ui, restored_value):
    dialog, _view = editing_ui
    editable_raster.pixels[0]["new_value"] = restored_value
    dialog.set_recode_pixel_table()
    dialog.update_recode_pixel_table()
    assert editable_raster.pixels[0]["new_value"] is None
    assert not editable_raster.old_new_value
    assert dialog.recodePixelTable.item(0, 3).text() == ""


def test_painting_reuses_integer_limits_per_band(editable_raster, monkeypatch):
    import ThRasE.core.editing as editing

    reads = []
    original = editing.raster_integer_limits

    def read_limits(path, band):
        reads.append(band)
        return original(path, band)

    monkeypatch.setattr(editing, "raster_integer_limits", read_limits)
    editable_raster.old_new_value = {1: 2}
    for x in (0.5, 1.5, 2.5):
        assert editable_raster.edit_from_pixel_picker(Pixel(x, 3.5)) == 1
    assert reads == [1]
    other_band = LayerToEdit(editable_raster.qgs_layer, 2)
    monkeypatch.setattr(LayerToEdit, "current", other_band)
    other_band.old_new_value = {9: 8}
    assert other_band.edit_from_pixel_picker(Pixel(0.5, 3.5)) == 9
    assert reads == [1, 2]


def test_polygon_candidates_are_clipped_and_lazy(editable_raster, monkeypatch):
    import inspect

    constructed = []
    original = QgsGeometry.fromPointXY

    def make_point(point):
        constructed.append(point)
        return original(point)

    monkeypatch.setattr(QgsGeometry, "fromPointXY", make_point)
    pixels = editable_raster.pixels_in_geometry(QgsGeometry.fromRect(QgsRectangle(-100000, -100000, 100000, 100000)))
    assert inspect.isgenerator(pixels)
    assert not constructed
    first = next(pixels)
    assert len(constructed) == 1
    selected = [first, *pixels]
    assert len(selected) == 16
    assert len(constructed) == 16
    assert all(editable_raster.check_point_inside_layer(pixel) for pixel in selected)


def test_packed_manual_values_are_rejected(tmp_path, editable_raster):
    path = tmp_path / "packed.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 4, 4, 1, gdal.GDT_Byte, options=["NBITS=2"])
    ds.SetGeoTransform((0, 1, 0, 4, 0, -1))
    ds.GetRasterBand(1).Fill(0)
    ds = None
    layer = QgsRasterLayer(str(path), "packed")
    packed = LayerToEdit(layer, 1)
    packed.data_provider.setEditable(True)
    try:
        with pytest.raises(ValueError, match="NBITS=2"):
            packed.edit_pixel(Pixel(0.5, 3.5), 4)
        assert packed.get_pixel_value_from_xy(0.5, 3.5) == 0
    finally:
        packed.data_provider.setEditable(False)


def test_invalid_mapping_is_rejected_before_any_polygon_write(editable_raster, monkeypatch):
    from qgis.core import QgsFeature

    editable_raster.old_new_value = {1: 2, 9: 300}
    writes = []
    monkeypatch.setattr(editable_raster.data_provider, "writeBlock", lambda *args: writes.append(args))
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromRect(QgsRectangle(0, 0, 4, 4)))
    editable_raster.edit_from_polygon_picker(feature)
    assert not writes
    assert not editable_raster.pixel_log_store
    assert not editable_raster.data_provider.isEditable()


@pytest.mark.usefixtures("plugin", "thrase_dialog")
class TestEditingTools:
    def test_line_edit(self, tmp_path, load_yaml_mapping):
        # original source tif
        src = pytest.tests_data_dir / "test_data.tif"

        # Load YAML mapping
        yml_path = pytest.tests_data_dir / "test_data_thrase.yaml"
        _, mapping = load_yaml_mapping(yml_path)
        assert mapping

        # Load the existing line fixture and pick its single feature
        vline = load_layer(str(pytest.tests_data_dir / "line.gpkg"), name="line")
        assert vline is not None and vline.isValid()
        vfeat = next(vline.getFeatures())

        ### create and apply the changes of the original tif to manual review and use for testing
        # saved_path = pytest.tests_data_dir / "test_data_line.tif"
        # saved_path.write_bytes(src.read_bytes())
        # layer_saved = load_layer(str(saved_path), name="test_data_line")
        # assert layer_saved is not None and layer_saved.isValid()
        # # Setup LayerToEdit for the layer and apply edit using the provided line.gpkg
        # lte_saved = LayerToEdit(layer_saved, band=1)
        # lte_saved.setup_pixel_table()
        # lte_saved.old_new_value = mapping
        # LayerToEdit.current = lte_saved
        # # Apply line-based edit and capture edited pixels
        # edited = LayerToEdit.current.edit_from_line_picker(vfeat, line_buffer=1.5)
        # assert edited, "No pixels edited by line; test cannot proceed"
        ### load test_data_line.tif
        saved_path = pytest.tests_data_dir / "test_data_line.tif"
        saved_test_data = load_layer(str(saved_path), name="test_data_line")
        assert saved_test_data is not None and saved_test_data.isValid()

        # test data edited for testing
        test_data_to_edit_path = tmp_path / "test_data_edited.tif"
        test_data_to_edit_path.write_bytes(src.read_bytes())
        layer_data_to_edit = load_layer(str(test_data_to_edit_path), name="test_data_edited")
        assert layer_data_to_edit is not None and layer_data_to_edit.isValid()

        # Setup LayerToEdit for the on-the-fly layer (do not reload after edit)
        lte_to_test = LayerToEdit(layer_data_to_edit, band=1)
        lte_to_test.setup_pixel_table()
        lte_to_test.old_new_value = mapping
        LayerToEdit.current = lte_to_test

        # Apply the same line-based edit on-the-fly
        test_data_edited = LayerToEdit.current.edit_from_line_picker(vfeat, line_buffer=1.5)
        assert test_data_edited, "On-the-fly edit did not produce any pixel edits"

        # Finally, compare the two rasters by reading band arrays with GDAL
        _assert_rasters_equal(saved_test_data, layer_data_to_edit, band=1)

    def test_polygon_edit(self, tmp_path, load_yaml_mapping):
        # original source tif
        src = pytest.tests_data_dir / "test_data.tif"

        # Load YAML mapping
        yml_path = pytest.tests_data_dir / "test_data_thrase.yaml"
        _, mapping = load_yaml_mapping(yml_path)
        assert mapping

        # Load the existing polygon fixture and pick its single feature
        vpolygon = load_layer(str(pytest.tests_data_dir / "polygon.gpkg"), name="polygon")
        assert vpolygon is not None and vpolygon.isValid()
        vfeat = next(vpolygon.getFeatures())

        ### create and apply the changes of the original tif to manual review and use for testing
        # saved_path = pytest.tests_data_dir / "test_data_polygon.tif"
        # saved_path.write_bytes(src.read_bytes())
        # layer_saved = load_layer(str(saved_path), name="test_data_polygon")
        # assert layer_saved is not None and layer_saved.isValid()
        # # Setup LayerToEdit for the layer and apply edit using the provided polygon.gpkg
        # lte_saved = LayerToEdit(layer_saved, band=1)
        # lte_saved.setup_pixel_table()
        # lte_saved.old_new_value = mapping
        # LayerToEdit.current = lte_saved
        # # Apply polygon-based edit and capture edited pixels
        # edited = LayerToEdit.current.edit_from_polygon_picker(vfeat)
        # assert edited, "No pixels edited by polygon; test cannot proceed"
        ### load test_data_polygon.tif
        saved_path = pytest.tests_data_dir / "test_data_polygon.tif"
        saved_test_data = load_layer(str(saved_path), name="test_data_polygon")
        assert saved_test_data is not None and saved_test_data.isValid()

        # test data edited for testing
        test_data_to_edit_path = tmp_path / "test_data_edited.tif"
        test_data_to_edit_path.write_bytes(src.read_bytes())
        layer_data_to_edit = load_layer(str(test_data_to_edit_path), name="test_data_edited")
        assert layer_data_to_edit is not None and layer_data_to_edit.isValid()

        # Setup LayerToEdit for the on-the-fly layer (do not reload after edit)
        lte_to_test = LayerToEdit(layer_data_to_edit, band=1)
        lte_to_test.setup_pixel_table()
        lte_to_test.old_new_value = mapping
        LayerToEdit.current = lte_to_test

        # Apply the same polygon-based edit on-the-fly
        test_data_edited = LayerToEdit.current.edit_from_polygon_picker(vfeat)
        assert test_data_edited, "On-the-fly edit did not produce any pixel edits"

        # Finally, compare the two rasters by reading band arrays with GDAL
        _assert_rasters_equal(saved_test_data, layer_data_to_edit, band=1)

    def test_freehand_edit(self, tmp_path, load_yaml_mapping):
        # original source tif
        src = pytest.tests_data_dir / "test_data.tif"

        # Load YAML mapping
        yml_path = pytest.tests_data_dir / "test_data_thrase.yaml"
        _, mapping = load_yaml_mapping(yml_path)
        assert mapping

        # Load the existing freehand fixture and pick its single feature
        vfreehand = load_layer(str(pytest.tests_data_dir / "freehand.gpkg"), name="freehand")
        assert vfreehand is not None and vfreehand.isValid()
        vfeat = next(vfreehand.getFeatures())

        ### create and apply the changes of the original tif to manual review and use for testing
        # saved_path = pytest.tests_data_dir / "test_data_freehand.tif"
        # saved_path.write_bytes(src.read_bytes())
        # layer_saved = load_layer(str(saved_path), name="test_data_freehand")
        # assert layer_saved is not None and layer_saved.isValid()
        # # Setup LayerToEdit for the layer and apply edit using the provided freehand.gpkg
        # lte_saved = LayerToEdit(layer_saved, band=1)
        # lte_saved.setup_pixel_table()
        # lte_saved.old_new_value = mapping
        # LayerToEdit.current = lte_saved
        # # Apply freehand-based edit and capture edited pixels
        # edited = LayerToEdit.current.edit_from_freehand_picker(vfeat)
        # assert edited, "No pixels edited by freehand; test cannot proceed"
        ### load test_data_freehand.tif
        saved_path = pytest.tests_data_dir / "test_data_freehand.tif"
        saved_test_data = load_layer(str(saved_path), name="test_data_freehand")
        assert saved_test_data is not None and saved_test_data.isValid()

        # test data edited for testing
        test_data_to_edit_path = tmp_path / "test_data_edited.tif"
        test_data_to_edit_path.write_bytes(src.read_bytes())
        layer_data_to_edit = load_layer(str(test_data_to_edit_path), name="test_data_edited")
        assert layer_data_to_edit is not None and layer_data_to_edit.isValid()

        # Setup LayerToEdit for the on-the-fly layer (do not reload after edit)
        lte_to_test = LayerToEdit(layer_data_to_edit, band=1)
        lte_to_test.setup_pixel_table()
        lte_to_test.old_new_value = mapping
        LayerToEdit.current = lte_to_test

        # Apply the same freehand-based edit on-the-fly
        test_data_edited = LayerToEdit.current.edit_from_freehand_picker(vfeat)
        assert test_data_edited, "On-the-fly edit did not produce any pixel edits"

        # Finally, compare the two rasters by reading band arrays with GDAL
        _assert_rasters_equal(saved_test_data, layer_data_to_edit, band=1)

    def test_entire_thematic_raster_edit(self, tmp_path, load_yaml_mapping):
        # original source tif
        src = pytest.tests_data_dir / "test_data.tif"

        # Load YAML mapping
        yml_path = pytest.tests_data_dir / "test_data_thrase.yaml"
        _, mapping = load_yaml_mapping(yml_path)
        assert mapping

        ### create and apply the changes of the original tif to manual review and use for testing
        # saved_path = pytest.tests_data_dir / "test_data_whole_image.tif"
        # saved_path.write_bytes(src.read_bytes())
        # layer_saved = load_layer(str(saved_path), name="test_data_whole_image")
        # assert layer_saved is not None and layer_saved.isValid()
        # # Setup LayerToEdit for the layer and apply edit to entire thematic raster
        # lte_saved = LayerToEdit(layer_saved, band=1)
        # lte_saved.setup_pixel_table()
        # lte_saved.old_new_value = mapping
        # LayerToEdit.current = lte_saved
        # # Apply to entire thematic raster edit
        # LayerToEdit.current.edit_to_entire_thematic_raster()
        ### load test_data_whole_image.tif
        saved_path = pytest.tests_data_dir / "test_data_whole_image.tif"
        saved_test_data = load_layer(str(saved_path), name="test_data_whole_image")
        assert saved_test_data is not None and saved_test_data.isValid()

        # test data edited for testing
        test_data_to_edit_path = tmp_path / "test_data_edited.tif"
        test_data_to_edit_path.write_bytes(src.read_bytes())
        layer_data_to_edit = load_layer(str(test_data_to_edit_path), name="test_data_edited")
        assert layer_data_to_edit is not None and layer_data_to_edit.isValid()

        # Setup LayerToEdit for the on-the-fly layer
        lte_to_test = LayerToEdit(layer_data_to_edit, band=1)
        lte_to_test.setup_pixel_table()
        lte_to_test.old_new_value = mapping
        LayerToEdit.current = lte_to_test

        # Apply to entire thematic raster edit (processes all pixels at once using GDAL)
        assert LayerToEdit.current.edit_to_entire_thematic_raster() == 640

        # Reload the layer to ensure we're reading the updated file
        layer_data_to_edit.reload()

        # Finally, compare the two rasters by reading band arrays with GDAL
        _assert_rasters_equal(saved_test_data, layer_data_to_edit, band=1)

    def test_apply_from_classes(self, tmp_path, load_yaml_mapping, qgis_iface, qgis_parent):
        # original source tif
        src = pytest.tests_data_dir / "test_data.tif"

        # Load YAML mapping
        yml_path = pytest.tests_data_dir / "test_data_thrase.yaml"
        _, mapping = load_yaml_mapping(yml_path)
        assert mapping

        # Load the thematic classes file (test_data_2.tif)
        thematic_classes_path = pytest.tests_data_dir / "test_data_2.tif"
        assert thematic_classes_path.exists(), f"Missing test_data_2.tif in {pytest.tests_data_dir}"

        ### create and apply the changes of the original tif to manual review and use for testing
        # saved_path = pytest.tests_data_dir / "test_data_thematic_classes.tif"
        # saved_path.write_bytes(src.read_bytes())
        # layer_saved = load_layer(str(saved_path), name="test_data_thematic_classes")
        # assert layer_saved is not None and layer_saved.isValid()
        # # Setup LayerToEdit for the layer and apply edit using ApplyFromClassesOrMask
        # lte_saved = LayerToEdit(layer_saved, band=1)
        # lte_saved.setup_pixel_table()
        # lte_saved.old_new_value = mapping
        # LayerToEdit.current = lte_saved
        # # Load thematic classes file
        # thematic_classes_layer = load_layer(str(thematic_classes_path), name="test_data_2")
        # assert thematic_classes_layer is not None and thematic_classes_layer.isValid()
        # # Create dialog and apply
        # dialog = ApplyFromClassesOrMask(parent=qgis_parent)
        # dialog.MsgBar = lte_saved.qgs_layer  # Mock MsgBar for testing
        # dialog.setup_gui()
        # dialog.QCBox_LayerForMasking.setLayer(thematic_classes_layer)
        # dialog.QCBox_LayerForMaskingBand.setCurrentText("1")
        # # Select classes 42 and 46
        # for row_idx in range(dialog.PixelTable.rowCount()):
        #     class_value = int(dialog.PixelTable.item(row_idx, 1).text())
        #     if class_value in [42, 46]:
        #         dialog.PixelTable.item(row_idx, 2).setCheckState(Qt.Checked)
        # # Apply changes
        # dialog.RecordChangesInRegistry.setChecked(False)
        # dialog.apply()
        ### load test_data_thematic_classes.tif
        saved_path = pytest.tests_data_dir / "test_data_thematic_classes.tif"
        saved_test_data = load_layer(str(saved_path), name="test_data_thematic_classes")
        assert saved_test_data is not None and saved_test_data.isValid()

        # test data edited for testing
        test_data_to_edit_path = tmp_path / "test_data_edited.tif"
        test_data_to_edit_path.write_bytes(src.read_bytes())
        layer_data_to_edit = load_layer(str(test_data_to_edit_path), name="test_data_edited")
        assert layer_data_to_edit is not None and layer_data_to_edit.isValid()

        # Setup LayerToEdit for the on-the-fly layer
        lte_to_test = LayerToEdit(layer_data_to_edit, band=1)
        lte_to_test.setup_pixel_table()
        lte_to_test.old_new_value = mapping
        LayerToEdit.current = lte_to_test

        # Load thematic classes file
        thematic_classes_layer = load_layer(str(thematic_classes_path), name="test_data_2")
        assert thematic_classes_layer is not None and thematic_classes_layer.isValid()

        # Create dialog and configure
        dialog = ApplyFromClassesOrMask(parent=qgis_parent)

        # Mock MsgBar for the dialog
        class MockMsgBar:
            def pushMessage(self, *args, **kwargs):
                pass

        dialog.MsgBar = MockMsgBar()

        dialog.setup_gui()

        # Set the thematic file
        dialog.QCBox_LayerForMasking.setLayer(thematic_classes_layer)

        # Set band to 1
        dialog.QCBox_LayerForMaskingBand.setCurrentText("1")

        # Select classes 42 and 46 (checkState: 0=Unchecked, 2=Checked)
        for row_idx in range(dialog.PixelTable.rowCount()):
            class_value = int(dialog.PixelTable.item(row_idx, 1).text())
            if class_value in [42, 46]:
                dialog.PixelTable.item(row_idx, 2).setCheckState(Qt.CheckState.Checked)

        # Disable recording changes in registry for testing
        dialog.RecordChangesInRegistry.setChecked(False)

        # Apply changes
        dialog.apply()
        from ThRasE.thrase import ThRasE

        assert ThRasE.dialog.raster_recode_controller.last_result.changed_count == 408

        # Reload the layer to ensure we're reading the updated file
        layer_data_to_edit.reload()

        # Finally, compare the two rasters by reading band arrays with GDAL
        _assert_rasters_equal(saved_test_data, layer_data_to_edit, band=1)

    def test_apply_from_vector_mask(self, tmp_path, load_yaml_mapping, qgis_iface, qgis_parent):
        # original source tif
        src = pytest.tests_data_dir / "test_data.tif"

        # Load YAML mapping
        yml_path = pytest.tests_data_dir / "test_data_thrase.yaml"
        _, mapping = load_yaml_mapping(yml_path)
        assert mapping

        # Vector mask fixture: a polygon layer covering the area where the
        # recode must be applied (only polygons are supported as vector mask).
        vector_mask_path = pytest.tests_data_dir / "freehand.gpkg"
        assert vector_mask_path.exists(), f"Missing freehand.gpkg in {pytest.tests_data_dir}"

        ### create and apply the changes of the original tif to manual review and use for testing
        # saved_path = pytest.tests_data_dir / "test_data_vector_mask.tif"
        # saved_path.write_bytes(src.read_bytes())
        # layer_saved = load_layer(str(saved_path), name="test_data_vector_mask")
        # assert layer_saved is not None and layer_saved.isValid()
        # # Setup LayerToEdit for the layer and apply edit using ApplyFromClassesOrMask with a vector mask
        # lte_saved = LayerToEdit(layer_saved, band=1)
        # lte_saved.setup_pixel_table()
        # lte_saved.old_new_value = mapping
        # LayerToEdit.current = lte_saved
        # # Load the polygon vector mask
        # vector_mask_layer = load_layer(str(vector_mask_path), name="freehand")
        # assert vector_mask_layer is not None and vector_mask_layer.isValid()
        # # Create dialog and apply
        # dialog = ApplyFromClassesOrMask(parent=qgis_parent)
        # class MockMsgBar:
        #     def pushMessage(self, *args, **kwargs):
        #         pass
        # dialog.MsgBar = MockMsgBar()
        # dialog.setup_gui()
        # dialog.QCBox_LayerForMasking.setLayer(vector_mask_layer)
        # dialog.RecordChangesInRegistry.setChecked(False)
        # dialog.apply()
        ### load test_data_vector_mask.tif
        saved_path = pytest.tests_data_dir / "test_data_vector_mask.tif"
        saved_test_data = load_layer(str(saved_path), name="test_data_vector_mask")
        assert saved_test_data is not None and saved_test_data.isValid()

        # test data edited for testing
        test_data_to_edit_path = tmp_path / "test_data_edited.tif"
        test_data_to_edit_path.write_bytes(src.read_bytes())
        layer_data_to_edit = load_layer(str(test_data_to_edit_path), name="test_data_edited")
        assert layer_data_to_edit is not None and layer_data_to_edit.isValid()

        # Setup LayerToEdit for the on-the-fly layer
        lte_to_test = LayerToEdit(layer_data_to_edit, band=1)
        lte_to_test.setup_pixel_table()
        lte_to_test.old_new_value = mapping
        LayerToEdit.current = lte_to_test

        # Load the polygon vector mask
        vector_mask_layer = load_layer(str(vector_mask_path), name="freehand")
        assert vector_mask_layer is not None and vector_mask_layer.isValid()

        # Create dialog and configure
        dialog = ApplyFromClassesOrMask(parent=qgis_parent)

        # Mock MsgBar for the dialog
        class MockMsgBar:
            def pushMessage(self, *args, **kwargs):
                pass

        dialog.MsgBar = MockMsgBar()

        dialog.setup_gui()

        # Selecting a polygon vector layer triggers _setup_vector_mask: the band
        # selector is hidden and the PixelTable is populated with a single,
        # non-interactive row (no class selection is required in vector mode).
        dialog.QCBox_LayerForMasking.setLayer(vector_mask_layer)
        assert dialog.vector_mask_layer is vector_mask_layer, "Vector layer was not registered as the active mask"
        assert dialog.raster_mask_layer is None, "Raster mask state should be empty when a vector mask is selected"

        # Disable recording changes in registry for testing
        dialog.RecordChangesInRegistry.setChecked(False)

        # Apply changes (mask = rasterized polygons with the pixel-center rule)
        dialog.apply()
        from ThRasE.thrase import ThRasE

        assert ThRasE.dialog.raster_recode_controller.last_result.changed_count == 289

        # Reload the layer to ensure we're reading the updated file
        layer_data_to_edit.reload()

        # Finally, compare the two rasters by reading band arrays with GDAL
        _assert_rasters_equal(saved_test_data, layer_data_to_edit, band=1)


def _assert_rasters_equal(layer_a, layer_b, band=1):
    """Compare two rasters by reading the specified band as 2D arrays with GDAL and assert equality.
    Any mismatch causes the test to fail. A concise sample of differences is reported for debugging.
    """
    from ThRasE.utils.qgis_utils import get_source_from

    path_a = get_source_from(layer_a)
    path_b = get_source_from(layer_b)
    dsa = gdal.Open(path_a)
    dsb = gdal.Open(path_b)
    assert dsa is not None and dsb is not None, f"Failed to open rasters with GDAL: '{path_a}', '{path_b}'"

    ba = dsa.GetRasterBand(band).ReadAsArray()
    bb = dsb.GetRasterBand(band).ReadAsArray()
    assert ba.shape == bb.shape, f"Array shape mismatch: A={ba.shape} vs B={bb.shape}"

    gt_a = dsa.GetGeoTransform()
    gt_b = dsb.GetGeoTransform()
    proj_a = dsa.GetProjection()
    proj_b = dsb.GetProjection()
    del dsa, dsb

    # Georeference checks (tolerant on floating rounding)
    def _approx(a, b, tol=1e-9):
        return abs(a - b) <= tol * max(1.0, abs(a), abs(b))

    assert len(gt_a) == len(gt_b) and all(_approx(a, b) for a, b in zip(gt_a, gt_b, strict=False)), (
        f"GeoTransform mismatch: A={gt_a} vs B={gt_b}"
    )
    assert proj_a == proj_b, "Projection WKT mismatch between rasters"

    diff_mask = ba != bb
    if np.any(diff_mask):
        _ys, xs = np.nonzero(diff_mask)
        count = len(xs)
        pytest.fail(f"Rasters arrays differ at {count} pixel(s)")

    # No differences found -> rasters are equal on the requested band
    return None


def test_wait_process_restores_the_cursor_when_the_action_fails(thrase_dialog):
    """Manual edits run under @wait_process; a failure must not leave a busy cursor."""
    from qgis.PyQt.QtWidgets import QApplication

    from ThRasE.utils.system_utils import wait_process

    @wait_process
    def failing():
        raise ValueError("injected failure")

    assert QApplication.overrideCursor() is None
    failing()  # error_handler reports the failure instead of propagating it
    assert QApplication.overrideCursor() is None


def test_float_band_is_not_accepted_as_a_thematic_layer(tmp_path, editing_ui):
    """edit_layer reads the band's integer limits, which only integer bands have."""
    from qgis.core import QgsProject

    from ThRasE.utils.qgis_utils import is_integer_data_type

    dialog, _view = editing_ui
    path = tmp_path / "float.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), 2, 2, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform((0, 1, 0, 2, 0, -1))
    dataset = None
    layer = QgsRasterLayer(str(path), "float")
    assert layer.isValid()
    assert not is_integer_data_type(layer, band=1)

    project = QgsProject.instance()
    assert project is not None
    project.addMapLayer(layer)
    dialog.QCBox_LayerToEdit.setLayer(layer)
    dialog.select_layer_to_edit(layer)
    assert LayerToEdit.current is None
