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
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QWidget, QFileDialog
from qgis.PyQt.QtCore import pyqtSlot, Qt
from qgis.core import QgsProject

from ThRasE.utils.qgis_utils import load_and_select_filepath_in
from ThRasE.utils.system_utils import block_signals_to

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'layer_view_widget.ui'))


class LayerViewWidget(QWidget, FORM_CLASS):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.id = None
        self.pc_id = None
        self.is_active = False
        self.setupUi(self)
        self.component_analysis_dialog = None
        # init as unactivated render widget for new instances
        self.widget_ActiveLayers.setHidden(True)
        self.disable()

    def setup_view_widget(self):
        self.render_widget.parent_view = self
        # settings the ActiveLayers widget
        self.QPBtn_ConfActiveLayers.clicked.connect(self.active_layers_widget)

        # ### active layer 1 ###
        # set properties to QgsMapLayerComboBox
        self.QCBox_RenderFile_1.setCurrentIndex(-1)
        # handle connect layer selection with render canvas
        self.QCBox_RenderFile_1.currentIndexChanged.connect(self.set_render_layer)
        # call to browse the render file
        self.QCBox_browseRenderFile_1.clicked.connect(lambda: self.fileDialog_browse(
            self.QCBox_RenderFile_1,
            dialog_title=self.tr("Select the above layer for this view"),
            dialog_types=self.tr("Raster or vector files (*.tif *.img *.gpkg *.shp);;All files (*.*)"),
            layer_type="any"))
        # edit layer properties
        self.layerStyleEditor_1.clicked.connect(self.render_widget.layer_style_editor)
        # handle connect layer transparency
        self.layerTransparency_1.valueChanged.connect(self.update_layer_transparency)

        # ### active layer 2 ###
        # set properties to QgsMapLayerComboBox
        self.QCBox_RenderFile_2.setCurrentIndex(-1)
        # handle connect layer selection with render canvas
        self.QCBox_RenderFile_2.currentIndexChanged.connect(self.set_render_layer)
        # call to browse the render file
        self.QCBox_browseRenderFile_2.clicked.connect(lambda: self.fileDialog_browse(
            self.QCBox_RenderFile_2,
            dialog_title=self.tr("Select the middle layer for this view"),
            dialog_types=self.tr("Raster or vector files (*.tif *.img *.gpkg *.shp);;All files (*.*)"),
            layer_type="any"))
        # edit layer properties
        self.layerStyleEditor_2.clicked.connect(self.render_widget.layer_style_editor)
        # handle connect layer transparency
        self.layerTransparency_2.valueChanged.connect(self.update_layer_transparency)

        # ### active layer 3 ###
        # set properties to QgsMapLayerComboBox
        self.QCBox_RenderFile_3.setCurrentIndex(-1)
        # handle connect layer selection with render canvas
        self.QCBox_RenderFile_3.currentIndexChanged.connect(self.set_render_layer)
        # call to browse the render file
        self.QCBox_browseRenderFile_3.clicked.connect(lambda: self.fileDialog_browse(
            self.QCBox_RenderFile_3,
            dialog_title=self.tr("Select the below layer for this view"),
            dialog_types=self.tr("Raster or vector files (*.tif *.img *.gpkg *.shp);;All files (*.*)"),
            layer_type="any"))
        # edit layer properties
        self.layerStyleEditor_3.clicked.connect(self.render_widget.layer_style_editor)
        # handle connect layer transparency
        self.layerTransparency_3.valueChanged.connect(self.update_layer_transparency)

    def clean(self):
        # clean first the component analysis dialog of this view widget
        if self.component_analysis_dialog is not None:
            self.component_analysis_dialog.clean()
            del self.component_analysis_dialog
        # clean this view widget and the layers loaded in PCs
        if self.pc_id is not None:
            for layer in self.render_widget.canvas.layers():
                QgsProject.instance().removeMapLayer(layer.id())
        self.disable()

    def enable(self):
        with block_signals_to(self.render_widget):
            # activate some parts of this view
            self.QLabel_ViewName.setEnabled(True)
            self.render_widget.setEnabled(True)
            #self.layerStyleEditor.setEnabled(True)
            self.render_widget.canvas.setCanvasColor(QColor(255, 255, 255))
            # set status for view widget
            self.is_active = True

    def disable(self):
        with block_signals_to(self.render_widget):
            self.render_widget.canvas.setLayers([])
            self.render_widget.canvas.clearCache()
            self.render_widget.canvas.refresh()
            self.render_widget.layer = None
            # deactivate some parts of this view
            self.QLabel_ViewName.setDisabled(True)
            self.render_widget.setDisabled(True)
            #self.layerStyleEditor.setDisabled(True)
            self.render_widget.canvas.setCanvasColor(QColor(245, 245, 245))
            # set status for view widget
            self.is_active = False

    def set_render_layer(self, layer):
        return
        self.render_widget.crs = layer_to_edit.crs()

        if not layer:
            self.disable()
            return

        self.enable()
        self.render_widget.render_layer(layer)

    @pyqtSlot()
    def fileDialog_browse(self, combo_box, dialog_title, dialog_types, layer_type):
        file_path, _ = QFileDialog.getOpenFileName(self, dialog_title, "", dialog_types)
        if file_path != '' and os.path.isfile(file_path):
            # load to qgis and update combobox list
            load_and_select_filepath_in(combo_box, file_path, layer_type)

            self.set_render_layer(combo_box.currentLayer())

    def active_layers_widget(self):
        # open to close
        if self.widget_ActiveLayers.isVisible():
            self.widget_ActiveLayers.setHidden(True)
            self.QPBtn_ConfActiveLayers.setArrowType(Qt.DownArrow)
        # close to open
        else:
            self.widget_ActiveLayers.setVisible(True)
            self.QPBtn_ConfActiveLayers.setArrowType(Qt.UpArrow)

    def update_layer_transparency(self, transparency):
        print(transparency)

