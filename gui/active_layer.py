# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ThRasE
                                 A QGIS plugin
 ThRasE is a Qgis plugin for Thematic Raster Edition
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
from pathlib import Path

from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QWidget, QFileDialog
from qgis.PyQt.QtCore import pyqtSlot
from qgis.core import QgsMapLayer

from ThRasE.utils.system_utils import block_signals_to
from ThRasE.utils.qgis_utils import load_and_select_filepath_in, StyleEditorDialog

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'active_layer.ui'))


class ActiveLayer(QWidget, FORM_CLASS):

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.id = None  # position: 1=upper, 2=intermediate, 3=lower
        self.layer = None
        self.parent_view = None  # view window instance
        self.render_widget = None  # the render_widget for this view window
        self.is_active = False  # enable and rendering a layer
        self.opacity = 100  # store the opacity value

        self.setupUi(self)

    def setup_gui(self, id, parent_view):
        self.id = id
        self.parent_view = parent_view
        self.render_widget = parent_view.render_widget
        parent_view.active_layers.append(self)
        # set properties to QgsMapLayerComboBox
        self.QCBox_RenderFile.setCurrentIndex(-1)
        # handle connect layer selection with render canvas
        self.QCBox_RenderFile.currentIndexChanged.connect(lambda: self.set_render_layer(self.QCBox_RenderFile.currentLayer()))
        self.QCBox_RenderFile.setToolTip("{} layer".format({1: "upper", 2: "intermediate", 3: "lower"}[self.id]))
        # call to browse the render file
        self.QCBox_browseRenderFile.clicked.connect(lambda: self.fileDialog_browse(
            self.QCBox_RenderFile,
            dialog_title=self.tr("Select the {} layer for this view".format({1: "upper", 2: "intermediate", 3: "lower"}[self.id])),
            dialog_types=self.tr("Raster or vector files (*.tif *.img *.gpkg *.shp);;All files (*.*)"),
            layer_type="any"))
        # edit layer properties
        self.layerStyleEditor.setDisabled(True)
        self.layerStyleEditor.clicked.connect(self.layer_style_editor)
        # on /off layer
        self.OnOffActiveLayer.toggled.connect(self.on_off_layer)
        # handle connect layer opacity
        self.layerOpacity.setDisabled(True)
        self.layerOpacity.valueChanged[int].connect(self.update_layer_opacity)

    @pyqtSlot()
    def fileDialog_browse(self, combo_box, dialog_title, dialog_types, layer_type):
        file_path, _ = QFileDialog.getOpenFileName(self, dialog_title, "", dialog_types)
        if file_path != '' and os.path.isfile(file_path):
            # load to qgis and update combobox list
            load_and_select_filepath_in(combo_box, file_path, layer_type)

            self.set_render_layer(combo_box.currentLayer())

    def enable(self):
        with block_signals_to(self.render_widget):
            # activate some parts of this view
            self.layerStyleEditor.setEnabled(True)
            self.layerOpacity.setEnabled(True)
            # set status for view widget
            self.is_active = True
            self.parent_view.update()

    def disable(self):
        with block_signals_to(self.render_widget):
            # deactivate some parts of this view
            self.layerStyleEditor.setDisabled(True)
            self.layerOpacity.setDisabled(True)
            # set status for view widget
            self.is_active = False
            self.parent_view.update()

    @pyqtSlot()
    def layer_style_editor(self):
        style_editor_dlg = StyleEditorDialog(self.layer, self.render_widget.canvas, self.parent())
        if style_editor_dlg.exec_():
            style_editor_dlg.apply()

    @pyqtSlot()
    def set_render_layer(self, layer):
        if not layer:
            self.disable()
            self.layer = None
            self.render_widget.update_render_layers()
            return

        self.layer = layer
        self.enable()
        self.render_widget.update_render_layers()
        if self.layer.type() == QgsMapLayer.VectorLayer:
            self.layerOpacity.setValue(int(self.layer.opacity()*100))
        else:
            self.layerOpacity.setValue(int(self.layer.renderer().opacity()*100))

    @pyqtSlot(bool)
    def on_off_layer(self, checked):
        if checked and self.layer:
            self.enable()
        else:
            self.disable()

        self.render_widget.update_render_layers()

    @pyqtSlot(int)
    def update_layer_opacity(self, opacity):
        if self.layer:
            if self.layer.type() == QgsMapLayer.VectorLayer:
                self.layer.setOpacity(opacity/100.0)
            else:
                self.layer.renderer().setOpacity(opacity/100.0)
            #if hasattr(self.layer, "setCacheImage"):
            #    self.layer.setCacheImage(None)
            self.layer.triggerRepaint()

            from ThRasE.gui.main_dialog import ThRasEDialog
            same_layer_in_others_active_layer = \
                [active_layer for active_layer in [al for als in [view_widget.active_layers for view_widget in ThRasEDialog.view_widgets] for al in als]
                 if active_layer.is_active and active_layer != self and active_layer.layer == self.layer]

            for active_layer in same_layer_in_others_active_layer:
                with block_signals_to(active_layer.layerOpacity):
                    active_layer.layerOpacity.setValue(opacity)

            self.opacity = opacity
