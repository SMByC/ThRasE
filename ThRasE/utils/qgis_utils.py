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

import os
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from math import isnan
from pathlib import Path

from osgeo import gdal
from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsPalettedRasterRenderer,
    QgsPointXY,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsRasterRange,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapLayerComboBox, QgsRendererPropertiesDialog, QgsRendererRasterPropertiesWidget
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QEventLoop, QSettings
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog
from qgis.utils import iface


@dataclass
class ReleasedRasterLayer:
    """State needed to reconnect a raster layer after replacing its file."""

    layer: QgsRasterLayer
    source: str
    name: str
    provider_type: str
    renderer: object | None
    user_nodata: tuple[tuple[QgsRasterRange, ...], ...]
    use_source_nodata: tuple[bool, ...]
    provider_resampling_enabled: bool | None
    zoomed_in_resampling: object | None
    zoomed_out_resampling: object | None
    provider_dpi: int | None


@dataclass
class ReleasedRasterResources:
    layers: tuple[ReleasedRasterLayer, ...]
    canvases: tuple[tuple[QgsMapCanvas, bool], ...]


def is_integer_data_type(layer, band=1):
    """Check if the raster layer data type for the given band is integer or byte.
    Uses the Qgis.DataType enum name to detect integer types generically,
    compatible across different QGIS/GDAL versions (including Int8, etc.).
    """
    data_type = layer.dataProvider().dataType(band)
    try:
        type_name = Qgis.DataType(data_type).name
    except ValueError:
        return False
    return type_name in {"Byte", "Int8", "UInt16", "Int16", "UInt32", "Int32", "UInt64", "Int64"}


def get_source_from(item):
    """Get the source/path of a QgsMapLayer or the current layer in a QgsMapLayerComboBox.
    Returns the filesystem path for local layers or the full datasource URI for remote/special layers.
    """
    layer = item.currentLayer() if isinstance(item, QgsMapLayerComboBox) else item
    if layer and layer.isValid():
        source = layer.source().split("|layername")[0]
        if os.path.isfile(source):
            return source
        # for remote/non-filesystem layers return the full source as identifier
        return layer.source()
    return ""


def valid_file_selected_in(combo_box):
    if combo_box.currentLayer() is None:
        return False
    if combo_box.currentLayer().isValid():
        return True
    else:
        combo_box.setCurrentIndex(-1)
        return False


def get_loaded_layer(source):
    # return the loaded layer in Qgis that matches the source
    # whatever the name of the layer
    for layer in QgsProject.instance().mapLayers().values():
        if layer.source() == source:
            return layer


def load_and_select_layer_in(source, combo_box, layer_name=None, add_to_legend=True):
    if not source:
        combo_box.setCurrentIndex(-1)
        return None
    qgslayer = get_loaded_layer(source)
    # try to load the layer if not already in QGIS
    if qgslayer is None:
        qgslayer = load_layer(source, name=layer_name, add_to_legend=add_to_legend)
        if qgslayer is None or not qgslayer.isValid():
            return None
    # select the exact layer in combobox
    combo_box.setLayer(qgslayer)

    return qgslayer


def add_layer(layer, add_to_legend=True):
    QgsProject.instance().addMapLayer(layer, add_to_legend)


def browse_dialog_to_load_file(parent, combo_box, dialog_title, file_filters, msg_bar=None, add_to_legend=True):
    """Open a file dialog, load the chosen file into QGIS and select it in `combo_box`.

    Returns the loaded layer, or None when the dialog was cancelled or the file
    could not be loaded, so the caller can keep track of the layers it added.
    """
    file_path, _ = QFileDialog.getOpenFileName(parent, dialog_title, "", file_filters)
    if file_path != "" and os.path.isfile(file_path):
        qgslayer = load_and_select_layer_in(file_path, combo_box, add_to_legend=add_to_legend)
        if not qgslayer:
            (msg_bar or iface.messageBar()).pushMessage(
                f"Could not load the layer: {file_path}", level=Qgis.MessageLevel.Warning, duration=10
            )
        return qgslayer
    return None


RASTER_EXTENSIONS = (".tif", ".tiff", ".vrt", ".img", ".jp2", ".asc", ".nc", ".hdf", ".ecw", ".dt2")
VECTOR_EXTENSIONS = (".shp", ".gpkg", ".geojson", ".json", ".kml", ".gml", ".csv", ".xlsx", ".ods", ".dxf", ".tab")


def detect_provider(source):
    """Detect the provider key and layer class from a file path or datasource URI."""
    s = source.lower().strip()

    # Local filesystem files (let QGIS auto-detect the best provider)
    ext = os.path.splitext(s)[1]
    if ext:
        if ext in RASTER_EXTENSIONS:
            return None, QgsRasterLayer
        if ext in VECTOR_EXTENSIONS:
            return None, QgsVectorLayer

    # Google Earth Engine
    if "type=xyz" in s and "url=https://earthengine.googleapis.com" in s:
        if "EE" in QgsProviderRegistry.instance().providerList():
            return "EE", QgsRasterLayer
        else:
            iface.messageBar().pushMessage(
                "ThRasE",
                "Google Earth Engine plugin is required to load this layer, install and configure it.",
                level=Qgis.MessageLevel.Warning,
                duration=20,
            )
            return None, None

    # OGC services
    if "type=xyz" in s or "provider=xyz" in s:
        return "wms", QgsRasterLayer
    if "service=wms" in s or "request=getmap" in s or "contextualwmslegend" in s or "contextualwmslegen" in s:
        return "wms", QgsRasterLayer
    if "service=wmts" in s or "tilematrixset" in s:
        return "wms", QgsRasterLayer
    if "service=wfs" in s or "typename=" in s or "provider=wfs" in s:
        return "wfs", QgsVectorLayer
    if "service=wcs" in s or "coverage=" in s or "coverageid=" in s:
        return "wcs", QgsRasterLayer

    # Databases
    if (
        s.startswith("postgresql://")
        or "provider=postgres" in s
        or ("dbname=" in s and ("table=" in s or "schema=" in s))
    ):
        return "postgres", QgsVectorLayer
    if "spatialite" in s or "provider=spatialite" in s or (".sqlite" in s and "table=" in s):
        return "spatialite", QgsVectorLayer

    # ArcGIS REST services
    if "mapserver" in s or "arcgismapserver" in s:
        return "arcgismapserver", QgsRasterLayer
    if "featureserver" in s or "arcgisfeatureserver" in s:
        return "arcgisfeatureserver", QgsVectorLayer

    # Vector tile datasource URIs
    if "provider=vectortile" in s or "type=vtpk" in s or "type=mbtiles" in s or "vectortile" in s:
        return "vectortile", QgsVectorLayer

    # Remote direct file URLs
    if s.startswith("http://") or s.startswith("https://") or "url=http" in s:
        if any(ext in s for ext in RASTER_EXTENSIONS):
            return "gdal", QgsRasterLayer
        if any(ext in s for ext in VECTOR_EXTENSIONS):
            return "ogr", QgsVectorLayer
        return "wms", QgsRasterLayer

    return None, None


def load_layer(source, name=None, add_to_legend=True):
    """Load a layer from a file path or remote datasource URI and add it to the project."""
    name = name or (os.path.splitext(os.path.basename(source))[0] if os.path.isfile(source) else "Remote Layer")

    provider_key, layer_class = detect_provider(source)
    qgslayer = (
        (layer_class(source, name, provider_key) if provider_key else layer_class(source, name))
        if layer_class
        else None
    )

    if qgslayer and qgslayer.isValid():
        QgsProject.instance().addMapLayer(qgslayer, add_to_legend)
        return qgslayer

    return None


def unload_layer(source):
    layers_loaded = QgsProject.instance().mapLayers().values()
    for layer_loaded in layers_loaded:
        if source == get_source_from(layer_loaded):
            QgsProject.instance().removeMapLayer(layer_loaded.id())


def _refresh_layer_to_edit_providers(layers, *, released=False):
    """Keep cached provider references from retaining a replaced GDAL dataset."""
    try:
        from ThRasE.core.editing import LayerToEdit
    except ImportError:
        return
    layer_ids = {layer.id() for layer in layers}
    for instance_key, layer_to_edit in list(LayerToEdit.instances.items()):
        try:
            layer_id = layer_to_edit.qgs_layer.id()
        except RuntimeError:
            del LayerToEdit.instances[instance_key]
            continue
        if layer_id in layer_ids:
            layer_to_edit.data_provider = None if released else layer_to_edit.qgs_layer.dataProvider()


def _map_canvases():
    canvases = []
    try:
        canvases.append(iface.mapCanvas())
    except (AttributeError, RuntimeError):
        pass
    canvases.extend(widget for widget in QApplication.allWidgets() if isinstance(widget, QgsMapCanvas))
    unique_canvases = []
    seen = set()
    for canvas in canvases:
        if canvas is None or id(canvas) in seen:
            continue
        seen.add(id(canvas))
        unique_canvases.append(canvas)
    return tuple(unique_canvases)


def _freeze_raster_canvases(existing_states=None, *, timeout_ms=5_000):
    unique_canvases = _map_canvases()

    original_states = {id(canvas): frozen for canvas, frozen in existing_states or ()}
    states = []
    try:
        for canvas in unique_canvases:
            states.append((canvas, original_states.get(id(canvas), canvas.isFrozen())))
            canvas.freeze(True)
            canvas.stopRendering()
        deadline = time.monotonic() + timeout_ms / 1000
        while any(canvas.isDrawing() for canvas, _frozen in states):
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out while stopping active QGIS raster rendering")
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 25)
    except Exception:
        _restore_raster_canvases(states)
        raise
    return tuple(states)


def _restore_raster_canvases(states):
    for canvas, was_frozen in states:
        try:
            canvas.freeze(was_frozen)
        except RuntimeError:
            pass


def _release_raster_layer_states(states):
    layers = [state.layer for state in states]
    _refresh_layer_to_edit_providers(layers, released=True)
    for state in states:
        state.layer.setDataSource("", state.name, state.provider_type, False)


def _same_local_file(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        first_path = os.path.normcase(os.path.realpath(os.path.abspath(first)))
        second_path = os.path.normcase(os.path.realpath(os.path.abspath(second)))
        return first_path == second_path


def release_raster_layers_for_source(source, additional_layers=()):
    """Release every matching QGIS GDAL provider so files can be replaced on Windows."""
    candidates = list(QgsProject.instance().mapLayers().values()) + list(additional_layers)
    for canvas in _map_canvases():
        try:
            candidates.extend(canvas.layers())
        except RuntimeError:
            continue
    states = []
    seen = set()
    for layer in candidates:
        if layer is None or layer.id() in seen or layer.type() != Qgis.LayerType.Raster:
            continue
        seen.add(layer.id())
        layer_source = get_source_from(layer)
        if not layer_source or not os.path.isfile(layer_source):
            continue
        if not _same_local_file(layer_source, source):
            continue
        renderer = layer.renderer()
        provider = layer.dataProvider()
        user_nodata = tuple(
            tuple(QgsRasterRange(value_range) for value_range in provider.userNoDataValues(band))
            for band in range(1, layer.bandCount() + 1)
        )
        use_source_nodata = tuple(provider.useSourceNoDataValue(band) for band in range(1, layer.bandCount() + 1))
        provider_resampling_enabled = (
            provider.isProviderResamplingEnabled() if hasattr(provider, "isProviderResamplingEnabled") else None
        )
        zoomed_in_resampling = (
            provider.zoomedInResamplingMethod() if hasattr(provider, "zoomedInResamplingMethod") else None
        )
        zoomed_out_resampling = (
            provider.zoomedOutResamplingMethod() if hasattr(provider, "zoomedOutResamplingMethod") else None
        )
        provider_dpi = provider.dpi() if hasattr(provider, "dpi") else None
        states.append(
            ReleasedRasterLayer(
                layer=layer,
                source=layer.source(),
                name=layer.name(),
                provider_type=layer.providerType() or "gdal",
                renderer=renderer.clone() if renderer is not None else None,
                user_nodata=user_nodata,
                use_source_nodata=use_source_nodata,
                provider_resampling_enabled=provider_resampling_enabled,
                zoomed_in_resampling=zoomed_in_resampling,
                zoomed_out_resampling=zoomed_out_resampling,
                provider_dpi=provider_dpi,
            )
        )

    canvas_states = _freeze_raster_canvases()
    resources = ReleasedRasterResources(tuple(states), canvas_states)
    try:
        _release_raster_layer_states(states)
    except Exception as error:
        try:
            restore_released_raster_layers(resources)
        except Exception:
            _restore_raster_canvases(canvas_states)
        raise RuntimeError(f"Unable to release QGIS raster providers before commit: {error}") from error
    return resources


def restore_released_raster_layers(resources):
    """Reconnect released layers, preserving their renderer and reporting every failure."""
    restored = []
    failures = []
    for state in resources.layers:
        try:
            state.layer.setDataSource(state.source, state.name, state.provider_type, False)
            if not state.layer.isValid():
                layer_error = state.layer.error().summary() if state.layer.error() is not None else "invalid layer"
                raise RuntimeError(layer_error or "invalid layer")
            if state.renderer is not None:
                state.layer.setRenderer(state.renderer.clone())
            provider = state.layer.dataProvider()
            for band, (user_nodata, use_source_nodata) in enumerate(
                zip(state.user_nodata, state.use_source_nodata, strict=True),
                start=1,
            ):
                provider.setUserNoDataValue(band, list(user_nodata))
                provider.setUseSourceNoDataValue(band, use_source_nodata)
            if state.zoomed_in_resampling is not None and hasattr(provider, "setZoomedInResamplingMethod"):
                provider.setZoomedInResamplingMethod(state.zoomed_in_resampling)
            if state.zoomed_out_resampling is not None and hasattr(provider, "setZoomedOutResamplingMethod"):
                provider.setZoomedOutResamplingMethod(state.zoomed_out_resampling)
            if state.provider_resampling_enabled is not None and hasattr(provider, "enableProviderResampling"):
                provider.enableProviderResampling(state.provider_resampling_enabled)
            if state.provider_dpi is not None and hasattr(provider, "setDpi"):
                provider.setDpi(state.provider_dpi)
            if hasattr(state.layer, "setCacheImage"):
                state.layer.setCacheImage(None)
            state.layer.triggerRepaint()
            restored.append(state.layer)
        except Exception as error:
            failures.append(f"{state.name}: {error}")
    _refresh_layer_to_edit_providers(restored)
    _restore_raster_canvases(resources.canvases)
    # The reconnected layers were repainted above, so only the canvases that were
    # frozen need a refresh; reloading every layer of the project is not needed.
    for canvas, _was_frozen in resources.canvases:
        try:
            canvas.refresh()
        except RuntimeError:
            pass
    if failures:
        raise RuntimeError("; ".join(failures))
    return restored


def _run_file_transaction(operation, *args):
    """Run pure GDAL transaction I/O off the GUI thread while excluding user input."""
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="thrase-raster-commit") as executor:
        future = executor.submit(operation, *args)
        while not future.done():
            completed, _pending = wait((future,), timeout=0.025)
            if not completed:
                QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 25)
        return future.result()


def finalize_commit_off_thread(receipt):
    """Remove a committed edit's backup and lock without blocking GUI event processing."""
    from ThRasE.core.raster_recode import finalize_commit

    return _run_file_transaction(finalize_commit, receipt)


def commit_staged_and_reload(result, additional_layers=()):
    """Release QGIS handles, commit, and restore the original on reload failure."""
    from ThRasE.core.raster_recode import (
        RasterRecodeError,
        RasterRecodeRecoveryError,
        commit_staged,
        rollback_commit,
    )

    states = release_raster_layers_for_source(result.source_path, additional_layers)
    try:
        receipt = _run_file_transaction(commit_staged, result)
    except Exception as commit_error:
        try:
            restore_released_raster_layers(states)
        except Exception as restore_error:
            error_type = (
                RasterRecodeRecoveryError if isinstance(commit_error, RasterRecodeRecoveryError) else RasterRecodeError
            )
            raise error_type(
                f"The raster commit failed and QGIS could not reconnect the original layer: {restore_error}"
            ) from commit_error
        raise

    try:
        restored = restore_released_raster_layers(states)
    except Exception as reload_error:
        # The edited files are in place but QGIS cannot read them: put the original back
        # before anything else uses the raster, then reconnect the layers and clean up.
        try:
            states.canvases = _freeze_raster_canvases(states.canvases)
            _release_raster_layer_states(states.layers)
            _run_file_transaction(rollback_commit, receipt)
            restore_released_raster_layers(states)
            finalize_commit_off_thread(receipt)
        except Exception as rollback_error:
            _restore_raster_canvases(states.canvases)
            raise RasterRecodeRecoveryError(
                "The raster was replaced but QGIS could not reload it, and automatic rollback failed. "
                f'The original raster remains at "{receipt.backup_path}": {rollback_error}'
            ) from reload_error
        raise RasterRecodeError(
            f"QGIS could not reload the edited raster, so the original raster was restored: {reload_error}"
        ) from reload_error
    return receipt, restored


@dataclass
class GlobalEditOutcome:
    """What happened after a staged global edit replaced the raster on disk.

    The raster is already edited when this is returned, so nothing here is a
    reason to discard the staged files; every field is something to report.
    """

    edited_count: int
    retained_backups: tuple[str, ...] = ()
    cleanup_error: Exception | None = None
    leftovers: tuple[str, ...] = ()
    recovery_error: Exception | None = None
    registry_error: Exception | None = None


def commit_and_reconcile(result, layer_to_edit, *, record_changes, registry_pixels, registry_widget=None):
    """Replace the raster with a staged edit, reload QGIS, clean up, and update the registry.

    This is the whole post-staging sequence, shared by the background controller
    and the synchronous edit method so both behave identically.  It raises only
    when the raster could not be replaced or reloaded, which is the one case where
    the caller must discard the staged files.  Anything that can go wrong once the
    edit is safely in place is reported through the returned outcome instead.
    """
    from ThRasE.core.raster_recode import RasterRecodeRecoveryError, find_transaction_leftovers

    receipt, _restored_layers = commit_staged_and_reload(result, additional_layers=(layer_to_edit.qgs_layer,))
    outcome = GlobalEditOutcome(edited_count=result.changed_count)
    try:
        outcome.retained_backups = finalize_commit_off_thread(receipt)
    except RasterRecodeRecoveryError as error:
        # The raster is edited but its transaction files need manual attention.
        # Leave the registry untouched so the reported state stays reproducible.
        outcome.recovery_error = error
        return outcome
    except Exception as error:
        outcome.cleanup_error = error
        outcome.leftovers = find_transaction_leftovers(receipt.source_path)

    try:
        if record_changes and result.changes is not None:
            layer_to_edit.store_global_edit_changes(result.changes, result.geotransform)
        else:
            layer_to_edit.reconcile_registry(result.registry_values, registry_pixels)
        if registry_widget is not None and layer_to_edit.pixel_log_store:
            registry_widget.update_registry()
    except Exception as error:
        outcome.registry_error = error
    return outcome


def global_edit_memory_budget() -> int:
    """Return the global-edit processing-memory budget in bytes.

    Reads the advanced ``ThRasE/global_edit_memory_mib`` setting, treating a
    missing or non-positive value as unset, and falls back to the documented default.
    """
    from ThRasE.core.raster_recode import DEFAULT_MEMORY_BUDGET_BYTES

    default_memory_mib = DEFAULT_MEMORY_BUDGET_BYTES // (1024 * 1024)
    memory_budget_mib = QSettings().value("ThRasE/global_edit_memory_mib", default_memory_mib, type=int)
    if not memory_budget_mib or memory_budget_mib < 1:
        memory_budget_mib = default_memory_mib
    return memory_budget_mib * 1024 * 1024


def get_nodata_value(layer, band=1):
    if layer is not None:
        nodata = layer.dataProvider().sourceNoDataValue(band)
        if not isnan(nodata):
            return nodata


def unset_the_nodata_value(layer):
    dataset = None
    try:
        dataset = gdal.Open(get_source_from(layer), gdal.GA_Update)
        if dataset is None:
            return 1

        for band_number in range(1, dataset.RasterCount + 1):
            if dataset.GetRasterBand(band_number).DeleteNoDataValue() != gdal.CE_None:
                return 1
        return 0
    except Exception:
        return 1
    finally:
        dataset = None


# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, "ui", "style_editor.ui"))


class StyleEditorDialog(QDialog, FORM_CLASS):
    def __init__(self, layer, canvas, parent=None):
        QDialog.__init__(self)
        self.setupUi(self)
        self.layer = layer

        self.setWindowTitle(f"{self.layer.name()} - Style Editor")

        if self.layer.type() == Qgis.LayerType.Vector:
            self.StyleEditorWidget = QgsRendererPropertiesDialog(self.layer, QgsStyle(), True, parent)

        if self.layer.type() == Qgis.LayerType.Raster:
            self.StyleEditorWidget = QgsRendererRasterPropertiesWidget(self.layer, canvas, parent)

        self.scrollArea.setWidget(self.StyleEditorWidget)

        self.DialogButtons.button(QDialogButtonBox.StandardButton.Cancel).clicked.connect(self.reject)
        self.DialogButtons.button(QDialogButtonBox.StandardButton.Ok).clicked.connect(self.accept)
        self.DialogButtons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)

    def apply(self):
        self.StyleEditorWidget.apply()
        self.layer.triggerRepaint()


def apply_symbology(rlayer, rband, symbology):
    """Apply symbology to raster layer using Paletted/Unique values"""
    paletted_classes = []
    for name, value, color in symbology:
        paletted_classes.append(
            QgsPalettedRasterRenderer.Class(value, QColor(color[0], color[1], color[2], color[3]), name)
        )

    renderer = QgsPalettedRasterRenderer(rlayer.dataProvider(), rband, paletted_classes)
    # Set renderer for raster layer
    rlayer.setRenderer(renderer)

    # set the opacity to the layer based on the opacity set in layer toolbar UI
    from ThRasE.gui.main_dialog import ThRasEDialog

    layer_toolbar = next(
        (
            layer_toolbar
            for layer_toolbar in [
                lt for lts in [view_widget.layer_toolbars for view_widget in ThRasEDialog.view_widgets] for lt in lts
            ]
            if layer_toolbar.layer == rlayer
        ),
        False,
    )
    if layer_toolbar:
        if rlayer.type() == Qgis.LayerType.Vector:
            rlayer.setOpacity(layer_toolbar.opacity / 100.0)
        else:
            rlayer.renderer().setOpacity(layer_toolbar.opacity / 100.0)

    # Repaint
    if hasattr(rlayer, "setCacheImage"):
        rlayer.setCacheImage(None)
    rlayer.triggerRepaint()


def add_color_value_to_symbology(renderer, new_value, new_color, new_label=None):
    """
    Add a new color/value pair to the raster layer's symbology.

    Parameters:
        renderer: QgsRasterRenderer or QgsSingleBandPseudoColorRenderer
        new_value: int - The value to add to the symbology; floats are converted to integers
        new_color: QColor or str - The color to associate with the value (QColor object or color name/hex)
        new_label: str, optional - Label for the new value (defaults to "Value {new_value}")

    Returns:
        The new symbology modified with the new color/value pair
    """
    if not renderer:
        return None

    new_value = int(new_value)

    # Convert color to QColor if string
    if isinstance(new_color, str):
        new_color = QColor(new_color)

    # Set default label if not provided
    if new_label is None:
        new_label = f"{new_value}"

    if isinstance(renderer, QgsPalettedRasterRenderer):
        classes = renderer.classes()
        # Check if value already exists
        if any(cls.value == new_value for cls in classes):
            return renderer
        # Add new class
        new_class = QgsPalettedRasterRenderer.Class(new_value, new_color, new_label)
        classes.append(new_class)
        # Create and return new renderer
        return QgsPalettedRasterRenderer(renderer.input(), renderer.band(), classes)

    elif isinstance(renderer, QgsSingleBandPseudoColorRenderer):
        # Get shader and color ramp
        shader = renderer.shader()
        if not isinstance(shader, QgsRasterShader):
            return None
        color_ramp = shader.rasterShaderFunction()
        if not isinstance(color_ramp, QgsColorRampShader):
            return None
        color_ramp_items = color_ramp.colorRampItemList()
        # Check if value already exists
        if any(item.value == new_value for item in color_ramp_items):
            return renderer
        # Add new item
        new_item = QgsColorRampShader.ColorRampItem(new_value, new_color, new_label)
        color_ramp_items.append(new_item)
        # Sort items by value
        color_ramp_items.sort(key=lambda x: x.value)
        # Create new color ramp shader with Exact Interpolation
        new_color_ramp_shader = QgsColorRampShader()
        new_color_ramp_shader.setColorRampType(QgsColorRampShader.Type.Exact)
        new_color_ramp_shader.setColorRampItemList(color_ramp_items)
        # Set Equal Interval mode by defining min/max values
        if color_ramp_items:
            min_value = min(item.value for item in color_ramp_items)
            max_value = max(item.value for item in color_ramp_items)
            new_color_ramp_shader.setMinimumValue(min_value)
            new_color_ramp_shader.setMaximumValue(max_value)
        # Create new shader
        new_shader = QgsRasterShader()
        new_shader.setRasterShaderFunction(new_color_ramp_shader)
        # Create and return new renderer
        new_renderer = QgsSingleBandPseudoColorRenderer(renderer.input(), renderer.band(), new_shader)
        return new_renderer

    else:
        return None


def get_pixel_centroid(x, y):
    """Get the centroid of the pixel where the point is located"""
    from ThRasE.core.editing import LayerToEdit

    bounds = LayerToEdit.current.bounds
    pixel_width = LayerToEdit.current.qgs_layer.rasterUnitsPerPixelX()
    pixel_height = LayerToEdit.current.qgs_layer.rasterUnitsPerPixelY()

    col = int((x - bounds[0]) / pixel_width)
    row = int((bounds[3] - y) / pixel_height)

    centroid_x = bounds[0] + (col + 0.5) * pixel_width
    centroid_y = bounds[3] - (row + 0.5) * pixel_height

    return QgsPointXY(centroid_x, centroid_y)
