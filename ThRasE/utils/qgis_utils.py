# -*- coding: utf-8 -*-
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
import platform
from math import isnan
from pathlib import Path
from subprocess import call

from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox
from qgis.PyQt import uic
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsRendererPropertiesDialog, QgsRendererRasterPropertiesWidget, QgsMapLayerComboBox
from qgis.core import QgsProject, QgsProviderRegistry, QgsRasterLayer, QgsVectorLayer, Qgis, QgsStyle, QgsMapLayer, \
                      QgsPointXY, QgsPalettedRasterRenderer, QgsSingleBandPseudoColorRenderer, QgsColorRampShader, \
                      QgsRasterShader
from qgis.utils import iface


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
    return "Int" in type_name or "Byte" in type_name


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
            iface.messageBar().pushMessage("ThRasE",
                "Google Earth Engine plugin is required to load this layer, install and configure it.",
                level=Qgis.MessageLevel.Warning, duration=20)
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
    if s.startswith("postgresql://") or "provider=postgres" in s or (
            "dbname=" in s and ("table=" in s or "schema=" in s)):
        return "postgres", QgsVectorLayer
    if "spatialite" in s or "provider=spatialite" in s or (
            ".sqlite" in s and "table=" in s):
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
    qgslayer = (layer_class(source, name, provider_key) if provider_key else layer_class(source, name)) if layer_class else None

    if qgslayer and qgslayer.isValid():
        QgsProject.instance().addMapLayer(qgslayer, add_to_legend)
        return qgslayer

    return None


def unload_layer(source):
    layers_loaded = QgsProject.instance().mapLayers().values()
    for layer_loaded in layers_loaded:
        if source == get_source_from(layer_loaded):
            QgsProject.instance().removeMapLayer(layer_loaded.id())


def get_nodata_value(layer, band=1):
    if layer is not None:
        nodata = layer.dataProvider().sourceNoDataValue(band)
        if not isnan(nodata):
            return nodata


def unset_the_nodata_value(layer):
    cmd = ['gdal_edit' if platform.system() == 'Windows' else 'gdal_edit.py',
           '"{}"'.format(get_source_from(layer)), "-unsetnodata"]
    return_code = call(" ".join(cmd), shell=True)
    return return_code


# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'style_editor.ui'))


class StyleEditorDialog(QDialog, FORM_CLASS):
    def __init__(self, layer, canvas, parent=None):
        QDialog.__init__(self)
        self.setupUi(self)
        self.layer = layer

        self.setWindowTitle("{} - Style Editor".format(self.layer.name()))

        if self.layer.type() == QgsMapLayer.LayerType.VectorLayer:
            self.StyleEditorWidget = QgsRendererPropertiesDialog(self.layer, QgsStyle(), True, parent)

        if self.layer.type() == QgsMapLayer.LayerType.RasterLayer:
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
        paletted_classes.append(QgsPalettedRasterRenderer.Class(value, QColor(color[0], color[1], color[2], color[3]), name))

    renderer = QgsPalettedRasterRenderer(rlayer.dataProvider(), rband, paletted_classes)
    # Set renderer for raster layer
    rlayer.setRenderer(renderer)

    # set the opacity to the layer based on the opacity set in layer toolbar UI
    from ThRasE.gui.main_dialog import ThRasEDialog
    layer_toolbar = next(
        (layer_toolbar for layer_toolbar in
         [lt for lts in [view_widget.layer_toolbars for view_widget in ThRasEDialog.view_widgets] for lt in lts]
         if layer_toolbar.layer == rlayer), False)
    if layer_toolbar:
        if rlayer.type() == QgsMapLayer.LayerType.VectorLayer:
            rlayer.setOpacity(layer_toolbar.opacity / 100.0)
        else:
            rlayer.renderer().setOpacity(layer_toolbar.opacity / 100.0)

    # Repaint
    if hasattr(rlayer, 'setCacheImage'):
        rlayer.setCacheImage(None)
    rlayer.triggerRepaint()


def add_color_value_to_symbology(renderer, new_value, new_color, new_label=None):
    """
    Add a new color/value pair to the raster layer's symbology.

    Parameters:
        renderer: QgsRasterRenderer or QgsSingleBandPseudoColorRenderer
        new_value: float or int - The value to add to the symbology
        new_color: QColor or str - The color to associate with the value (QColor object or color name/hex)
        new_label: str, optional - Label for the new value (defaults to "Value {new_value}")

    Returns:
        The new symbology modified with the new color/value pair
    """
    if not renderer:
        return None

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
