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
from qgis.PyQt.QtCore import Qt, QTimer, QSettings
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QWidget, QGridLayout
from qgis.gui import QgsMapToolPan, QgsMapCanvas

from ThRasE.utils.system_utils import block_signals_to


class RenderWidget(QWidget):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.setupUi()
        self.active_layers = None  # instances of active layers
        self.detection_layer = None
        self.crs = None

    def setupUi(self):
        gridLayout = QGridLayout(self)
        gridLayout.setContentsMargins(0, 0, 0, 0)
        self.canvas = QgsMapCanvas()
        self.canvas.setCanvasColor(QColor(255, 255, 255))
        self.canvas.setStyleSheet("border: 0px;")
        settings = QSettings()
        self.canvas.enableAntiAliasing(settings.value("/qgis/enable_anti_aliasing", False, type=bool))
        self.setMinimumSize(15, 15)
        # action pan and zoom
        self.default_point_tool = DefaultPointTool(self)
        self.canvas.setMapTool(self.default_point_tool, clean=True)

        gridLayout.addWidget(self.canvas)

    def update_render_layers(self):
        with block_signals_to(self):
            # set the CRS of the canvas view
            if self.crs:
                self.canvas.setDestinationCrs(self.crs)
            # get all valid activated layers
            valid_layers = [active_layer.layer for active_layer in self.active_layers if active_layer.is_active]
            if len(valid_layers) == 0:
                self.canvas.setLayers([])
                self.canvas.refresh()
                return
            # set to canvas
            self.canvas.setLayers(valid_layers)
            # set init extent from other view if any is activated else set layer extent
            from ThRasE.gui.main_dialog import ThRasEDialog
            others_extents = [view_widget.render_widget.canvas.extent() for view_widget in ThRasEDialog.view_widgets
                              if view_widget.is_active and view_widget != self and not view_widget.render_widget.canvas.extent().isEmpty()]

            if others_extents:
                extent = others_extents[0]
                self.update_canvas_to(extent)
            else:
                self.canvas.setExtent(valid_layers[0].extent())

            self.canvas.refresh()

    def update_canvas_to(self, new_extent):
        with block_signals_to(self.canvas):
            self.canvas.setExtent(new_extent)
            self.canvas.refresh()


class DefaultPointTool(QgsMapToolPan):
    def __init__(self, render_widget):
        QgsMapToolPan.__init__(self, render_widget.canvas)
        self.render_widget = render_widget

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.RightButton:
            QgsMapToolPan.canvasReleaseEvent(self, event)
            self.render_widget.parent_view.canvas_changed()

    def canvasPressEvent(self, event):
        if event.button() != Qt.RightButton:
            QgsMapToolPan.canvasPressEvent(self, event)

    def wheelEvent(self, event):
        QgsMapToolPan.wheelEvent(self, event)
        QTimer.singleShot(10, self.render_widget.parent_view.canvas_changed)

    def keyReleaseEvent(self, event):
        if event.key() in [Qt.Key_Up, Qt.Key_Down, Qt.Key_Right, Qt.Key_Left, Qt.Key_PageUp, Qt.Key_PageDown]:
            QTimer.singleShot(10, self.render_widget.parent_view.canvas_changed)

