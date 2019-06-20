# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ThRasE
                                 A QGIS plugin
 ThRasE is a Thematic Raster Editor plugin of Qgis
                              -------------------
        copyright            : (C) 2019 by Xavier Corredor Llano, SMByC
        email                : xcorredorl@ideam.gov.co
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
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer, Qgis, QgsStyle, QgsMapLayer, QgsRasterShader, \
    QgsColorRampShader, QgsSingleBandPseudoColorRenderer
from qgis.utils import iface


def get_file_path_of_layer(layer):
    if layer and layer.isValid():
        return os.path.realpath(layer.source().split("|layername")[0])
    return ""


def valid_file_selected_in(combo_box):
    if combo_box.currentLayer() is not None and combo_box.currentLayer().isValid():
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
        iface.messageBar().pushMessage("ThRasE", "Error, please select a valid file", level=Qgis.Warning)
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
        iface.messageBar().pushMessage("ThRasE", "Could not load layer: {}".format(file_path))

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
    """ Apply symbology to raster layer """
    # See: QgsRasterRenderer* QgsSingleBandPseudoColorRendererWidget::renderer()
    # https://github.com/qgis/QGIS/blob/master/src/gui/raster/qgssinglebandpseudocolorrendererwidget.cpp
    # Get raster shader
    raster_shader = QgsRasterShader()
    # Color ramp shader
    color_ramp_shader = QgsColorRampShader()
    # Loop over Fmask values and add to color item list
    color_ramp_item_list = []
    for name, value, color in symbology:
        # Color ramp item - color, label, value
        color_ramp_item = QgsColorRampShader.ColorRampItem(value, QColor(color[0], color[1], color[2], color[3]), name)
        color_ramp_item_list.append(color_ramp_item)

    # After getting list of color ramp items
    color_ramp_shader.setColorRampItemList(color_ramp_item_list)
    # Exact color ramp
    color_ramp_shader.setColorRampType('EXACT')
    # Add color ramp shader to raster shader
    raster_shader.setRasterShaderFunction(color_ramp_shader)
    # Create color renderer for raster layer
    renderer = QgsSingleBandPseudoColorRenderer(rlayer.dataProvider(), rband, raster_shader)
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
