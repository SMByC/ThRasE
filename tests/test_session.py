"""Session saving and provider-aware datasource persistence."""

import inspect

import pytest
import yaml
from osgeo import ogr
from qgis.core import QgsCoordinateReferenceSystem, QgsProject, QgsVectorLayer
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtGui import QCloseEvent
from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox

from ThRasE.core.editing import LayerToEdit
from ThRasE.gui.main_dialog import ThRasEDialog
from ThRasE.gui.navigation_dialog import NavigationDialog
from ThRasE.utils.qgis_utils import (
    apply_symbology,
    load_and_select_layer_in,
    resolve_session_source,
    session_layer_source,
)


@pytest.mark.parametrize("failure", ["serialize", "replace"])
def test_failed_save_preserves_previous_yaml_and_destination(
    editable_raster, editing_ui, tmp_path, monkeypatch, failure
):
    import ThRasE.core.editing as editing

    dialog, _view = editing_ui
    destination = tmp_path / "session.yaml"
    destination.write_text("previous session", encoding="utf-8")
    editable_raster.config_file = str(destination)

    def fail_dump(data, stream, **kwargs):
        stream.write("partial output")
        raise OSError("disk full")

    def fail_replace(*args):
        raise OSError("replacement denied")

    if failure == "serialize":
        monkeypatch.setattr(editing.yaml, "dump", fail_dump)
    else:
        monkeypatch.setattr(editing.os, "replace", fail_replace)
    assert dialog.save_thrase_config() is False
    assert destination.read_text(encoding="utf-8") == "previous session"
    assert editable_raster.config_file == str(destination)
    assert not list(tmp_path.glob(".thrase-config-*"))


def test_successful_save_returns_success(editable_raster, editing_ui, tmp_path):
    path = tmp_path / "saved.yaml"
    assert editable_raster.save_config(str(path)) is True
    assert editable_raster.config_file == str(path)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(saved, dict)
    assert saved["thematic_file_to_edit"]["band"] == 1
    assert saved["recode_pixel_table"][0]["new_value"] == 7


def test_save_as_failure_keeps_current_destination(editable_raster, editing_ui, tmp_path, monkeypatch):
    dialog, _view = editing_ui
    editable_raster.config_file = "previous.yaml"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(tmp_path / "other.yaml"), ""))
    monkeypatch.setattr(editable_raster, "save_config", lambda _path: None)
    assert dialog.file_dialog_save_thrase_config() is False
    assert editable_raster.config_file == "previous.yaml"


def test_save_and_close_is_refused_when_writing_fails(editable_raster, editing_ui, tmp_path, monkeypatch):
    import ThRasE.core.editing as editing

    dialog, view = editing_ui
    editable_raster.config_file = str(tmp_path / "session.yaml")
    monkeypatch.setattr(dialog, "isVisible", lambda: True)
    monkeypatch.setattr(QMessageBox, "exec", lambda _self: QMessageBox.StandardButton.Save)

    def fail_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(editing.yaml, "dump", fail_dump)
    closed = []
    dialog.closingPlugin.connect(lambda: closed.append(True))
    event = QCloseEvent()
    dialog.closeEvent(event)
    assert not event.isAccepted()
    assert not closed
    assert view._edit_target is editable_raster
    assert not dialog.close_confirmed


def test_session_source_preserves_geopackage_sublayer(tmp_path, qgis_app, monkeypatch):
    path = tmp_path / "layers.gpkg"
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    ds.CreateLayer("first", geom_type=ogr.wkbPoint)
    ds.CreateLayer("second", geom_type=ogr.wkbPolygon)
    ds = None
    uri = str(path) + "|layername=second"
    layer = QgsVectorLayer(uri, "second", "ogr")
    saved = session_layer_source(layer, str(tmp_path / "session.yaml"))
    assert saved is not None
    assert "layername=second" in saved
    assert not saved.startswith(str(tmp_path))
    monkeypatch.chdir(tmp_path.parent)
    restored = resolve_session_source(saved, str(tmp_path / "session.yaml"), "ogr")
    combo = QgsMapLayerComboBox()
    loaded = load_and_select_layer_in(restored, combo, provider="ogr")
    assert loaded is not None
    assert loaded.source() == uri
    assert combo.currentLayer() == loaded
    combo.deleteLater()


def test_remote_session_source_is_not_treated_as_local(tmp_path, qgis_app):
    uri = "https://example.org/reference.tif"
    assert resolve_session_source(uri, str(tmp_path / "session.yaml"), "gdal") == uri


@pytest.mark.parametrize("extension", ["gpkg", "tif"])
def test_legacy_bare_relative_source_is_resolved_against_the_yaml(tmp_path, qgis_app, monkeypatch, extension):
    """Configurations written before ./-prefixed URIs stored a bare filename."""
    monkeypatch.chdir(tmp_path.parent)
    saved = f"reference.{extension}" + ("|layername=first" if extension == "gpkg" else "")
    restored = resolve_session_source(saved, str(tmp_path / "session.yaml"), "ogr")
    assert restored.startswith(str(tmp_path))
    assert restored.endswith(saved)


class _RecordingMessageBar:
    """Collects what the dialog reports, which a closed dialog would never show."""

    def __init__(self):
        self.messages = []

    def pushMessage(self, *args, **kwargs):
        self.messages.append(args[0] if args else "")

    def clearWidgets(self):
        pass


def _saved_navigation_session(editable_raster, editing_ui, tmp_path):
    """Build a navigation from a GeoPackage polygon layer and save the session."""
    dialog, view = editing_ui
    editable_raster.qgs_layer.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    apply_symbology(editable_raster.qgs_layer, editable_raster.band, editable_raster.symbology)
    project = QgsProject.instance()
    assert project is not None
    project.addMapLayer(editable_raster.qgs_layer)
    view.layer_toolbars[0].set_render_layer(editable_raster.qgs_layer)
    path = tmp_path / "navigation.gpkg"
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    ds.CreateLayer("unused", geom_type=ogr.wkbPoint)
    layer = ds.CreateLayer("boundary", geom_type=ogr.wkbPolygon)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON ((0 0, 4 0, 4 4, 0 4, 0 0))"))
    layer.CreateFeature(feature)
    feature = layer = ds = None
    vector = QgsVectorLayer(str(path) + "|layername=boundary", "boundary", "ogr")
    vector.setCrs(editable_raster.qgs_layer.crs())
    project.addMapLayer(vector)
    nav = NavigationDialog(dialog, layer_to_edit=editable_raster)
    editable_raster.navigation_dialog = nav
    nav.QCBox_BuildNavType.setCurrentText("polygons")
    nav.tileSize.setValue(2)
    nav.QCBox_VectorFile.setLayer(vector)
    nav.call_to_build_navigation()
    dialog.QPBtn_EnableNavigation.setChecked(True)
    original = tmp_path / "original.yaml"
    assert editable_raster.save_config(str(original)) is True
    config = yaml.safe_load(original.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    assert "layername=boundary" in config["navigation"]["vector_file"]
    return dialog, vector, path, config


def test_copied_session_restores_navigation_relative_to_yaml(editable_raster, editing_ui, tmp_path, monkeypatch):
    dialog, vector, _gpkg, config = _saved_navigation_session(editable_raster, editing_ui, tmp_path)
    copied = tmp_path / "copy.yaml"
    copied.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    inspect.unwrap(ThRasEDialog.restore_config)(dialog, str(copied), config)
    assert LayerToEdit.current is not None
    assert LayerToEdit.current.config_file == str(copied)
    assert LayerToEdit.current.navigation.is_valid
    assert LayerToEdit.current.navigation_dialog.QCBox_VectorFile.currentLayer().source() == vector.source()


def test_missing_navigation_vector_leaves_the_rest_of_the_session_open(
    editable_raster, editing_ui, tmp_path, monkeypatch
):
    dialog, vector, gpkg, config = _saved_navigation_session(editable_raster, editing_ui, tmp_path)
    project = QgsProject.instance()
    assert project is not None
    project.removeMapLayer(vector.id())
    gpkg.unlink()
    # Restore onto an existing session too: unavailable navigation must not retain old tiles.
    assert editable_raster.navigation.is_valid
    messages = _RecordingMessageBar()
    monkeypatch.setattr(dialog, "MsgBar", messages)
    saved = tmp_path / "session.yaml"
    saved.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert inspect.unwrap(ThRasEDialog.restore_config)(dialog, str(saved), config) is not False
    assert LayerToEdit.current is editable_raster
    assert not editable_raster.navigation.is_valid
    assert any("navigation vector file" in message for message in messages.messages)


@pytest.mark.parametrize("saved_tile", [None, 999])
def test_invalid_navigation_size_discards_previous_tiles(editable_raster, editing_ui, tmp_path, saved_tile):
    dialog, _vector, _gpkg, config = _saved_navigation_session(editable_raster, editing_ui, tmp_path)
    config["navigation"]["tile_size"] = 0
    config["navigation"]["current_tile_id"] = saved_tile
    assert inspect.unwrap(ThRasEDialog.restore_config)(dialog, str(tmp_path / "session.yaml"), config) is not False
    assert not editable_raster.navigation.is_valid
    assert not editable_raster.navigation.tiles
    assert editable_raster.navigation.current_tile is None
    assert not dialog.NavigationBlockWidgetControls.isEnabled()


def test_missing_saved_tile_starts_at_first_tile(editable_raster, editing_ui, tmp_path):
    dialog, _vector, _gpkg, config = _saved_navigation_session(editable_raster, editing_ui, tmp_path)
    del config["navigation"]["current_tile_id"]
    assert inspect.unwrap(ThRasEDialog.restore_config)(dialog, str(tmp_path / "session.yaml"), config) is not False
    assert editable_raster.navigation.current_tile.idx == 1


def test_free_navigation_does_not_keep_previous_session_tiles(editable_raster, editing_ui, tmp_path):
    dialog, _vector, _gpkg, config = _saved_navigation_session(editable_raster, editing_ui, tmp_path)
    config["navigation"] = {"type": "free"}
    assert inspect.unwrap(ThRasEDialog.restore_config)(dialog, str(tmp_path / "session.yaml"), config) is not False
    assert not editable_raster.navigation.tiles
    assert not dialog.QPBtn_EnableNavigation.isChecked()


def test_unknown_saved_tile_restores_the_session_at_its_first_tile(editable_raster, editing_ui, tmp_path, monkeypatch):
    dialog, _vector, _gpkg, config = _saved_navigation_session(editable_raster, editing_ui, tmp_path)
    config["navigation"]["current_tile_id"] = 999
    messages = _RecordingMessageBar()
    monkeypatch.setattr(dialog, "MsgBar", messages)
    saved = tmp_path / "session.yaml"
    saved.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert inspect.unwrap(ThRasEDialog.restore_config)(dialog, str(saved), config) is not False
    assert LayerToEdit.current is not None
    navigation = LayerToEdit.current.navigation
    assert navigation.is_valid
    assert navigation.current_tile.idx == 1
    assert any("not part of the rebuilt navigation" in message for message in messages.messages)
