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
from qgis.PyQt.QtWidgets import QWidget
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject

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

    def setup_view_widget(self):
        self.render_widget.parent_view = self
        # settings the ActiveLayers widget
        self.QPBtn_ConfActiveLayers.clicked.connect(self.active_layers_widget)

        # ### init the three active layers ###
        self.widget_ActiveLayer_1.setup_gui(1)
        self.widget_ActiveLayer_2.setup_gui(2)
        self.widget_ActiveLayer_3.setup_gui(3)

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

    def active_layers_widget(self):
        # open to close
        if self.widget_ActiveLayers.isVisible():
            self.widget_ActiveLayers.setHidden(True)
            self.QPBtn_ConfActiveLayers.setArrowType(Qt.DownArrow)
        # close to open
        else:
            self.widget_ActiveLayers.setVisible(True)
            self.QPBtn_ConfActiveLayers.setArrowType(Qt.UpArrow)


