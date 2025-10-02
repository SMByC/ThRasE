# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ThRasE

 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2025 by Xavier Corredor Llano, SMByC
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
import pytest
import numpy as np
from osgeo import gdal

from qgis.PyQt.QtCore import Qt

from ThRasE.core.editing import LayerToEdit
from ThRasE.utils.qgis_utils import load_layer
from ThRasE.gui.apply_from_thematic_classes import ApplyFromThematicClasses


@pytest.mark.usefixtures("thrase_dialog_stub")
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

    def test_whole_image_edit(self, tmp_path, load_yaml_mapping):
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
        # # Setup LayerToEdit for the layer and apply edit to the whole image
        # lte_saved = LayerToEdit(layer_saved, band=1)
        # lte_saved.setup_pixel_table()
        # lte_saved.old_new_value = mapping
        # LayerToEdit.current = lte_saved
        # # Apply whole image edit
        # LayerToEdit.current.edit_whole_image()
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

        # Apply the whole image edit (processes all pixels at once using GDAL)
        LayerToEdit.current.edit_whole_image()

        # Reload the layer to ensure we're reading the updated file
        layer_data_to_edit.reload()

        # Finally, compare the two rasters by reading band arrays with GDAL
        _assert_rasters_equal(saved_test_data, layer_data_to_edit, band=1)

    def test_apply_from_thematic_classes(self, tmp_path, load_yaml_mapping, qgis_iface, qgis_parent):
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
        # # Setup LayerToEdit for the layer and apply edit using ApplyFromThematicClasses
        # lte_saved = LayerToEdit(layer_saved, band=1)
        # lte_saved.setup_pixel_table()
        # lte_saved.old_new_value = mapping
        # LayerToEdit.current = lte_saved
        # # Load thematic classes file
        # thematic_classes_layer = load_layer(str(thematic_classes_path), name="test_data_2")
        # assert thematic_classes_layer is not None and thematic_classes_layer.isValid()
        # # Create dialog and apply
        # dialog = ApplyFromThematicClasses(parent=qgis_parent)
        # dialog.MsgBar = lte_saved.qgs_layer  # Mock MsgBar for testing
        # dialog.setup_gui()
        # dialog.QCBox_ThematicFile.setLayer(thematic_classes_layer)
        # dialog.QCBox_band_ThematicFile.setCurrentText("1")
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
        dialog = ApplyFromThematicClasses(parent=qgis_parent)
        
        # Mock MsgBar for the dialog
        class MockMsgBar:
            def pushMessage(self, *args, **kwargs):
                pass
        dialog.MsgBar = MockMsgBar()
        
        dialog.setup_gui()
        
        # Set the thematic file
        dialog.QCBox_ThematicFile.setLayer(thematic_classes_layer)
        
        # Set band to 1
        dialog.QCBox_band_ThematicFile.setCurrentText("1")
        
        # Select classes 42 and 46 (checkState: 0=Unchecked, 2=Checked)
        for row_idx in range(dialog.PixelTable.rowCount()):
            class_value = int(dialog.PixelTable.item(row_idx, 1).text())
            if class_value in [42, 46]:
                dialog.PixelTable.item(row_idx, 2).setCheckState(2)  # Qt.Checked = 2
        
        # Disable recording changes in registry for testing
        dialog.RecordChangesInRegistry.setChecked(False)
        
        # Apply changes
        dialog.apply()
        
        # Reload the layer to ensure we're reading the updated file
        layer_data_to_edit.reload()

        # Finally, compare the two rasters by reading band arrays with GDAL
        _assert_rasters_equal(saved_test_data, layer_data_to_edit, band=1)


def _assert_rasters_equal(layer_a, layer_b, band=1):
    """Compare two rasters by reading the specified band as 2D arrays with GDAL and assert equality.
    Any mismatch causes the test to fail. A concise sample of differences is reported for debugging.
    """
    from ThRasE.utils.qgis_utils import get_file_path_of_layer
    path_a = get_file_path_of_layer(layer_a)
    path_b = get_file_path_of_layer(layer_b)
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
    assert (len(gt_a) == len(gt_b) and all(_approx(a, b) for a, b in zip(gt_a, gt_b))), \
        f"GeoTransform mismatch: A={gt_a} vs B={gt_b}"
    assert proj_a == proj_b, "Projection WKT mismatch between rasters"

    diff_mask = (ba != bb)
    if np.any(diff_mask):
        ys, xs = np.nonzero(diff_mask)
        count = len(xs)
        pytest.fail(f"Rasters arrays differ at {count} pixel(s)")

    # No differences found -> rasters are equal on the requested band
    return None

