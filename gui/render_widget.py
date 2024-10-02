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
from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QWidget, QGridLayout
from qgis.core import QgsCoordinateTransform, QgsProject
from qgis.gui import QgsMapToolPan, QgsMapCanvas
from qgis.utils import iface

from ThRasE.utils.system_utils import block_signals_to


class RenderWidget(QWidget):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.setupUi()
        self.active_layers = None  # instances of active layers
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
        self.default_point_tool = QgsMapToolPan(self.canvas)
        self.canvas.setMapTool(self.default_point_tool, clean=True)

        gridLayout.addWidget(self.canvas)

    def refresh(self):
        if self.active_layers is not None:
            [active_layer.layer.reload() for active_layer in self.active_layers if active_layer.is_active]
            [active_layer.layer.triggerRepaint() for active_layer in self.active_layers if active_layer.is_active]
        self.canvas.refreshAllLayers()

    def set_crs(self, crs):
        self.crs = crs
        self.update_render_layers()

    def update_render_layers(self):
        with block_signals_to(self):
            # set the CRS of the canvas view
            if self.crs:
                # use the crs of thematic raster to edit
                self.canvas.setDestinationCrs(self.crs)
            else:
                # use the crs set in Qgis
                self.canvas.setDestinationCrs(iface.mapCanvas().mapSettings().destinationCrs())
            # get all valid activated layers
            valid_layers = [active_layer.layer for active_layer in self.active_layers if active_layer.is_active]
            if len(valid_layers) == 0:
                self.canvas.setLayers([])
                self.refresh()
                return
            # set to canvas
            self.canvas.setLayers(valid_layers)
            # set init extent from other view if any is activated else set layer extent
            from ThRasE.gui.main_dialog import ThRasEDialog
            others_extents = [view_widget.render_widget.canvas.extent() for view_widget in ThRasEDialog.view_widgets
                              if view_widget.is_active and view_widget.render_widget != self
                              and not view_widget.render_widget.canvas.extent().isEmpty()]
            if others_extents:
                # set extent using the extent of the other valid view (or self) with at least one layer
                extent = others_extents[0]
                self.update_canvas_to(extent)
            elif self.canvas.extent().isEmpty():
                # first layer to render
                # set the extent using the extent of the Qgis project but first transform the crs if it is different
                new_layer = valid_layers[0]
                transform = QgsCoordinateTransform(new_layer.crs(), self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
                new_extent = transform.transformBoundingBox(new_layer.extent())
                self.canvas.setExtent(new_extent)

            self.refresh()

    def update_canvas_to(self, new_extent):
        with block_signals_to(self.canvas):
            self.canvas.setExtent(new_extent)
            self.refresh()
