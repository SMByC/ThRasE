# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ThRasE
 
 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2024 by Xavier Corredor Llano, SMByC
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
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsRendererPropertiesDialog, QgsRendererRasterPropertiesWidget
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer, Qgis, QgsStyle, QgsMapLayer, \
                      QgsPalettedRasterRenderer, QgsSingleBandPseudoColorRenderer, QgsColorRampShader, QgsRasterShader
from qgis.utils import iface


def get_file_path_of_layer(layer):
    if layer and layer.isValid():
        return layer.source().split("|layername")[0]
    return ""


def valid_file_selected_in(combo_box):
    if combo_box.currentLayer() is None:
        return False
    if combo_box.currentLayer().isValid():
        return True
    else:
        combo_box.setCurrentIndex(-1)
        return False


def get_layer_by_name(layer_name):
    layer = QgsProject.instance().mapLayersByName(layer_name)
    if layer:
        return layer[0]


def get_current_file_path_in(combo_box, show_message=True):
    file_path = get_file_path_of_layer(combo_box.currentLayer())
    if os.path.isfile(file_path):
        return file_path
    elif show_message:
        iface.messageBar().pushMessage("ThRasE", "Error, please select a valid file", level=Qgis.Warning, duration=5)
    return None


def load_and_select_filepath_in(combo_box, file_path, layer_name=None, add_to_legend=True):
    if not layer_name:
        layer_name = os.path.splitext(os.path.basename(file_path))[0]
    layer = get_layer_by_name(layer_name)
    # load
    if not layer:
        load_layer(file_path, name=layer_name, add_to_legend=add_to_legend)
    # select the sampling file in combobox
    selected_index = combo_box.findText(layer_name, Qt.MatchFixedString)
    combo_box.setCurrentIndex(selected_index)

    return get_layer_by_name(layer_name)


def add_layer(layer, add_to_legend=True):
    QgsProject.instance().addMapLayer(layer, add_to_legend)


def load_layer(file_path, name=None, add_to_legend=True):
    # first unload layer from qgis if exists
    unload_layer(file_path)

    name = name or os.path.splitext(os.path.basename(file_path))[0]
    # vector
    qgslayer = QgsVectorLayer(file_path, name, "ogr")
    if not qgslayer.isValid():
        # raster
        qgslayer = QgsRasterLayer(file_path, name, "gdal")

    # load
    if qgslayer.isValid():
        add_layer(qgslayer, add_to_legend)
    else:
        iface.messageBar().pushMessage("ThRasE", "Could not to load the layer '{}' no such file {}"
                                       .format(name, file_path), level=Qgis.Warning, duration=-1)
    return qgslayer


def unload_layer(layer_path):
    layers_loaded = QgsProject.instance().mapLayers().values()
    for layer_loaded in layers_loaded:
        if layer_path == get_file_path_of_layer(layer_loaded):
            QgsProject.instance().removeMapLayer(layer_loaded.id())


def get_nodata_value(layer, band=1):
    if layer is not None:
        nodata = layer.dataProvider().sourceNoDataValue(band)
        if not isnan(nodata):
            return nodata


def unset_the_nodata_value(layer):
    cmd = ['gdal_edit' if platform.system() == 'Windows' else 'gdal_edit.py',
           '"{}"'.format(get_file_path_of_layer(layer)), "-unsetnodata"]
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

        if self.layer.type() == QgsMapLayer.VectorLayer:
            self.StyleEditorWidget = QgsRendererPropertiesDialog(self.layer, QgsStyle(), True, parent)

        if self.layer.type() == QgsMapLayer.RasterLayer:
            self.StyleEditorWidget = QgsRendererRasterPropertiesWidget(self.layer, canvas, parent)

        self.scrollArea.setWidget(self.StyleEditorWidget)

        self.DialogButtons.button(QDialogButtonBox.Cancel).clicked.connect(self.reject)
        self.DialogButtons.button(QDialogButtonBox.Ok).clicked.connect(self.accept)
        self.DialogButtons.button(QDialogButtonBox.Apply).clicked.connect(self.apply)

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

    # set the opacity to the layer based on the opacity set in active layer UI
    from ThRasE.gui.main_dialog import ThRasEDialog
    active_layer = next(
        (active_layer for active_layer in
         [al for als in [view_widget.active_layers for view_widget in ThRasEDialog.view_widgets] for al in als]
         if active_layer.layer == rlayer), False)
    if active_layer:
        if rlayer.type() == QgsMapLayer.VectorLayer:
            rlayer.setOpacity(active_layer.opacity / 100.0)
        else:
            rlayer.renderer().setOpacity(active_layer.opacity / 100.0)

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
        new_color_ramp_shader.setColorRampType(QgsColorRampShader.Exact)
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
