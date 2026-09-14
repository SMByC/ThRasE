"""Editing-view ownership, map tools, canvas refresh and layer toolbars."""

import numpy as np
import pytest
from osgeo import gdal
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QEventLoop, QPoint, QTimer

from ThRasE.core.editing import LayerToEdit, Pixel
from ThRasE.gui.view_widget import (
    PickerFreehandTool,
    PickerLineTool,
    PickerPixelTool,
    PickerPolygonTool,
    ViewWidgetSingle,
)


def _wait_for_timers(milliseconds=220):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def test_target_switch_cancels_pending_edits_and_switches_history(editable_raster, editing_ui):
    dialog, view = editing_ui
    view.edit_logs["pixel"].add((Pixel(0.5, 3.5), 1))
    history = view.edit_logs["pixel"]
    calls = []
    view.schedule_edit(lambda: calls.append("wrong target"))
    other = LayerToEdit(editable_raster.qgs_layer, 2)
    dialog.change_edit_target(other)
    assert not view.UndoPixel.isEnabled()
    _wait_for_timers()
    assert not calls
    dialog.change_edit_target(editable_raster)
    assert view.edit_logs["pixel"] is history
    assert view.UndoPixel.isEnabled()


@pytest.mark.parametrize("switch_band", [False, True])
def test_undo_never_writes_to_a_different_target(editable_raster, editing_ui, tmp_path, switch_band):
    dialog, view = editing_ui
    pixel = Pixel(0.5, 3.5)
    previous = editable_raster.edit_from_pixel_picker(pixel)
    assert previous == 1
    view.edit_logs["pixel"].add((pixel, previous))
    if switch_band:
        other = LayerToEdit(editable_raster.qgs_layer, 2)
    else:
        path = tmp_path / "other.tif"
        ds = gdal.GetDriverByName("GTiff").Create(str(path), 4, 4, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((0, 1, 0, 4, 0, -1))
        ds.GetRasterBand(1).WriteArray(np.full((4, 4), 9, dtype=np.uint8))
        ds = None
        layer = QgsRasterLayer(str(path), "other")
        other = LayerToEdit(layer, 1)
    dialog.change_edit_target(other)
    view.go_to_history("undo", "pixel")
    assert other.get_pixel_value_from_pnt(pixel.qgs_point) == 9
    dialog.change_edit_target(editable_raster)
    view.go_to_history("undo", "pixel")
    assert editable_raster.get_pixel_value_from_pnt(pixel.qgs_point) == 1
    dialog.change_edit_target(other)
    view.go_to_history("redo", "pixel")
    assert other.get_pixel_value_from_pnt(pixel.qgs_point) == 9
    dialog.change_edit_target(editable_raster)
    view.go_to_history("redo", "pixel")
    assert editable_raster.get_pixel_value_from_pnt(pixel.qgs_point) == 7


def test_histories_are_independent_between_views(editable_raster, editing_ui):
    dialog, first = editing_ui
    second = ViewWidgetSingle(dialog)
    second.setup_view_widget()
    second.set_edit_target(editable_raster)
    first.edit_logs["pixel"].add((Pixel(0.5, 3.5), 1))
    assert not second.edit_logs["pixel"].can_be_undone()
    second.set_edit_target(None)
    second.deleteLater()


@pytest.mark.parametrize(
    "tool_class, tool_name",
    [
        (PickerLineTool, "line"),
        (PickerPolygonTool, "polygon"),
        (PickerFreehandTool, "freehand"),
    ],
)
@pytest.mark.parametrize("matches", [False, True])
def test_delayed_geometry_edit_and_history(editable_raster, editing_ui, tool_class, tool_name, matches):
    _dialog, view = editing_ui
    view.AutoClear.setChecked(False)
    if not matches:
        editable_raster.old_new_value = {2: 3}
    tool = tool_class(view)
    view.render_widget.canvas.setMapTool(tool)
    band = tool.line if tool_name == "line" else tool.rubber_band
    for point in (QgsPointXY(0, 4), QgsPointXY(1, 4), QgsPointXY(1, 3), QgsPointXY(0, 3)):
        band.addPoint(point)
    if tool_name == "line":
        tool.define_line()
    elif tool_name == "polygon":
        tool.define_polygon()
    else:
        tool.canvasReleaseEvent(None)
    assert editable_raster.get_pixel_value_from_xy(0.5, 3.5) == 1
    _wait_for_timers()
    assert not view._pending_edits
    if not matches:
        assert editable_raster.get_pixel_value_from_xy(0.5, 3.5) == 1
        assert not view.edit_logs[tool_name].can_be_undone()
        assert not getattr(
            view, {"line": "lines_drawn", "polygon": "polygons_drawn", "freehand": "freehand_drawn"}[tool_name]
        )
        return
    assert editable_raster.get_pixel_value_from_xy(0.5, 3.5) == 7
    assert view.edit_logs[tool_name].can_be_undone()
    view.go_to_history("undo", tool_name)
    assert editable_raster.get_pixel_value_from_xy(0.5, 3.5) == 1
    assert view.edit_logs[tool_name].can_be_redone()
    view.go_to_history("redo", tool_name)
    assert editable_raster.get_pixel_value_from_xy(0.5, 3.5) == 7


def test_pixel_picker_ignores_outside_click_and_records_inside_click(editable_raster, editing_ui):
    from types import SimpleNamespace

    _dialog, view = editing_ui
    canvas = view.render_widget.canvas
    canvas.resize(400, 400)
    canvas.setExtent(QgsRectangle(0, 0, 4, 4))
    tool = PickerPixelTool(view)
    canvas.setMapTool(tool)

    def click(x, y):
        screen = canvas.mapSettings().mapToPixel().transform(QgsPointXY(x, y))
        tool.edit(SimpleNamespace(pos=lambda: QPoint(round(screen.x()), round(screen.y()))))

    click(-0.5, 4.5)
    assert not view.edit_logs["pixel"].can_be_undone()
    click(0.5, 3.5)
    assert editable_raster.get_pixel_value_from_xy(0.5, 3.5) == 7
    assert view.edit_logs["pixel"].can_be_undone()


def test_edit_returns_before_queued_target_switch_is_dispatched(editable_raster, editing_ui):
    dialog, view = editing_ui
    other = LayerToEdit(editable_raster.qgs_layer, 2)
    QTimer.singleShot(0, lambda: dialog.change_edit_target(other))
    pixel = Pixel(0.5, 3.5)
    old_value = editable_raster.edit_from_pixel_picker(pixel)
    assert old_value == 1
    assert LayerToEdit.current is editable_raster
    history = view.edit_logs["pixel"]
    history.add((pixel, old_value))
    _wait_for_timers(0)
    assert LayerToEdit.current is other
    dialog.change_edit_target(editable_raster)
    assert view.edit_logs["pixel"] is history


def test_auto_clear_from_previous_target_does_not_clear_new_drawings(editable_raster, editing_ui):
    _dialog, view = editing_ui
    view.AutoClear.setChecked(True)
    view.trigger_auto_clear("polygon")
    view.set_edit_target(None)
    view.set_edit_target(editable_raster)
    from qgis.core import Qgis
    from qgis.gui import QgsRubberBand

    drawing = QgsRubberBand(view.render_widget.canvas, Qgis.GeometryType.Polygon)
    view.polygons_drawn.append(drawing)
    _wait_for_timers(550)
    assert view.polygons_drawn == [drawing]


def test_finishing_global_edit_does_not_reenable_removed_target(editable_raster, editing_ui):
    dialog, view = editing_ui
    dialog.set_global_edit_active(True)
    dialog.unset_thematic_layer_to_edit()
    dialog.set_global_edit_active(False)
    assert LayerToEdit.current is None
    assert not view.widget_EditingToolbar.isEnabled()
    assert not dialog.QGBox_GlobalEditTools.isEnabled()
    assert not dialog.SaveConfig.isEnabled()


def test_canvas_pan_does_not_reload_layers(editable_raster, editing_ui, monkeypatch):
    _dialog, view = editing_ui
    toolbar = view.layer_toolbars[0]
    toolbar.set_render_layer(editable_raster.qgs_layer)
    calls = []
    monkeypatch.setattr(editable_raster.qgs_layer, "reload", lambda: calls.append("reload"))
    view.render_widget.update_canvas_to(QgsRectangle(0, 0, 2, 2))
    assert not calls


def test_shared_layer_opacity_updates_both_models(editable_raster, editing_ui):
    _dialog, view = editing_ui
    first, second = view.layer_toolbars[:2]
    first.set_render_layer(editable_raster.qgs_layer)
    second.set_render_layer(editable_raster.qgs_layer)
    second.layerOpacity.setValue(30)
    assert first.opacity == second.opacity == 30
    assert first.layerOpacity.value() == 30
    editable_raster.setup_symbology()
    assert editable_raster.qgs_layer.renderer().opacity() == pytest.approx(0.3)


def test_zoom_to_reference_transforms_extent(editable_raster, editing_ui):
    _dialog, view = editing_ui
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "degrees", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromRect(QgsRectangle(10, 10, 11, 11)))
    provider = layer.dataProvider()
    assert provider is not None
    provider.addFeatures([feature])
    layer.updateExtents()
    toolbar = view.layer_toolbars[0]
    toolbar.set_render_layer(layer)
    canvas = view.render_widget.canvas
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    expected = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
    expected = expected.transformBoundingBox(layer.extent()).center()
    toolbar.zoom_to_layer()
    assert canvas.extent().center().x() == pytest.approx(expected.x())
    assert canvas.extent().center().y() == pytest.approx(expected.y())


def test_canvas_extent_is_transformed_when_target_crs_changes(editable_raster, editing_ui):
    _dialog, view = editing_ui
    view.render_widget.layer_toolbars = []
    canvas = view.render_widget.canvas
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    canvas.setExtent(QgsRectangle(10, 10, 11, 11))
    old_extent = canvas.extent()
    destination = QgsCoordinateReferenceSystem("EPSG:3857")
    expected = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), destination, QgsProject.instance())
    expected = expected.transformBoundingBox(old_extent).center()
    view.render_widget.set_crs(destination)
    assert canvas.extent().center().x() == pytest.approx(expected.x())
    assert canvas.extent().center().y() == pytest.approx(expected.y())


def test_removing_target_cancels_tools_and_discards_session(editable_raster, editing_ui):
    _dialog, view = editing_ui
    project = QgsProject.instance()
    assert project is not None
    project.addMapLayer(editable_raster.qgs_layer)
    view.use_pixels_picker_for_edit()
    calls = []
    view.schedule_edit(lambda: calls.append("edit"))
    layer_id = editable_raster.qgs_layer.id()
    project.removeMapLayer(layer_id)
    _wait_for_timers()
    assert not calls
    assert LayerToEdit.current is None
    assert (layer_id, 1) not in LayerToEdit.instances
    assert not view._pending_edits
    assert view.render_widget.canvas.mapTool() is view.render_widget.default_point_tool


@pytest.mark.parametrize(
    "tool_class, geometry_type",
    [
        (PickerLineTool, "line"),
        (PickerPolygonTool, "polygon"),
        (PickerFreehandTool, "freehand"),
    ],
)
def test_picker_geometry_cleanup_releases_scene_items(editable_raster, editing_ui, tool_class, geometry_type):
    _dialog, view = editing_ui
    canvas = view.render_widget.canvas
    baseline = len(canvas.scene().items())
    tool = tool_class(view)
    canvas.setMapTool(tool)
    for _ in range(10):
        if geometry_type == "line":
            tool.start_new_line()
        elif geometry_type == "polygon":
            tool.start_new_polygon()
        else:
            tool.start_new_freehand()
        assert len(canvas.scene().items()) == baseline + (2 if geometry_type == "polygon" else 1)
    view.set_edit_target(None)
    assert len(canvas.scene().items()) == baseline


def test_completed_polygon_cleanup_releases_scene_items(editable_raster, editing_ui):
    _dialog, view = editing_ui
    canvas = view.render_widget.canvas
    baseline = len(canvas.scene().items())
    tool = PickerPolygonTool(view)
    canvas.setMapTool(tool)
    for _ in range(10):
        assert tool.rubber_band is not None
        for point in (QgsPointXY(0, 0), QgsPointXY(1, 0), QgsPointXY(1, 1)):
            tool.rubber_band.addPoint(point)
        tool.define_polygon()
        view.clear_all_polygons_drawn()
        assert len(canvas.scene().items()) == baseline + 2
    view.set_edit_target(None)
    assert len(canvas.scene().items()) == baseline


@pytest.mark.parametrize("action", ["undo", "layer_toolbars", "editing_toolbars"])
def test_deferred_ui_callbacks_do_not_outlive_the_dialog(editable_raster, editing_ui, monkeypatch, action):
    import sys

    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    dialog, view = editing_ui
    view.layer_toolbars[0].set_render_layer(editable_raster.qgs_layer)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda _type, error, _traceback: errors.append(error))
    if action == "undo":
        pixel = Pixel(0.5, 3.5)
        previous = editable_raster.edit_from_pixel_picker(pixel)
        view.edit_logs["pixel"].add((pixel, previous))
        view.go_to_history("undo", "pixel")
    elif action == "layer_toolbars":
        view.toggle_layer_toolbars(True)
    else:
        view.toggle_editing_toolbars(True)
    dialog.closing_for_unload = True
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _wait_for_timers(70)
    assert not errors, [str(error) for error in errors]


def test_dispose_canvas_item_tolerates_an_already_released_item(editing_ui):
    """Releasing twice must not raise: cleanup paths can overlap."""
    from qgis.core import Qgis
    from qgis.gui import QgsRubberBand
    from qgis.PyQt import sip

    from ThRasE.utils.qgis_utils import dispose_canvas_item

    _dialog, view = editing_ui
    canvas = view.render_widget.canvas
    baseline = len(canvas.scene().items())
    band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
    assert len(canvas.scene().items()) == baseline + 1

    dispose_canvas_item(band)
    assert sip.isdeleted(band)
    assert len(canvas.scene().items()) == baseline
    dispose_canvas_item(band)
    dispose_canvas_item(None)
