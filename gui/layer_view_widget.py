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

from qgis.core import QgsProject
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QWidget
from qgis.PyQt.QtCore import Qt, pyqtSlot, QTimer
from qgis.PyQt.QtGui import QColor

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
        self.layer_to_edit = None  # TODO delete if use the class
        self.active_layers = []  # for save the active layers instances
        self.setupUi(self)
        # init as unactivated render widget for new instances
        self.widget_ActiveLayers.setHidden(True)
        self.disable()

    def setup_view_widget(self):
        self.render_widget.parent_view = self
        # settings the ActiveLayers widget
        self.QPBtn_ConfActiveLayers.clicked.connect(self.active_layers_widget)

        # ### init the three active layers ###
        self.widget_ActiveLayer_1.setup_gui(1, self)
        self.widget_ActiveLayer_2.setup_gui(2, self)
        self.widget_ActiveLayer_3.setup_gui(3, self)
        # save active layers in render widget
        self.render_widget.active_layers = self.active_layers

    def clean(self):
        # clean this view widget and the layers loaded in PCs
        if self.pc_id is not None:
            for layer in self.render_widget.canvas.layers():
                QgsProject.instance().removeMapLayer(layer.id())
        self.disable()

    def update(self):
        valid_layers = [active_layer.layer for active_layer in self.active_layers if active_layer.is_active]
        if len(valid_layers) > 0:
            self.enable()
        else:
            self.disable()
        self.QPBtn_ConfActiveLayers.setText("{} active layers".format(len(valid_layers)))

    def enable(self):
        with block_signals_to(self.render_widget):
            # activate some parts of this view
            self.QLabel_ViewName.setEnabled(True)
            self.render_widget.setEnabled(True)
            self.render_widget.canvas.setCanvasColor(QColor(255, 255, 255))
            # set status for view widget
            self.is_active = True

    def disable(self):
        with block_signals_to(self.render_widget):
            self.render_widget.canvas.setLayers([])
            self.render_widget.canvas.clearCache()
            self.render_widget.canvas.refresh()
            # deactivate some parts of this view
            self.QLabel_ViewName.setDisabled(True)
            self.render_widget.setDisabled(True)
            self.render_widget.canvas.setCanvasColor(QColor(245, 245, 245))
            # set status for view widget
            self.is_active = False

    def active_layers_widget(self):
        # open/close all active layers widgets
        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            # open to close
            if view_widget.widget_ActiveLayers.isVisible():
                view_widget.widget_ActiveLayers.setHidden(True)
                view_widget.QPBtn_ConfActiveLayers.setArrowType(Qt.DownArrow)
            # close to open
            else:
                view_widget.widget_ActiveLayers.setVisible(True)
                view_widget.QPBtn_ConfActiveLayers.setArrowType(Qt.UpArrow)
        # refresh all extents based on the first active view
        actives_view_widget = [view_widget for view_widget in ThRasEDialog.view_widgets if view_widget.is_active]
        if actives_view_widget:
            QTimer.singleShot(10, lambda: actives_view_widget[0].canvas_changed())

    @pyqtSlot()
    def canvas_changed(self):
        if self.is_active:
            new_extent = self.render_widget.canvas.extent()
            # update canvas for all view activated except this view
            from ThRasE.gui.main_dialog import ThRasEDialog
            for view_widget in ThRasEDialog.view_widgets:
                # for layer view widget in main dialog
                if view_widget.is_active and view_widget != self:
                    view_widget.render_widget.update_canvas_to(new_extent)

