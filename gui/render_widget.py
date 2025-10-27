# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ThRasE
 
 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2025 by Xavier Corredor Llano, SMByC
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
        self.layer_toolbars = None  # instances of layer toolbars
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
        if self.layer_toolbars is not None:
            [layer_toolbar.layer.reload() for layer_toolbar in self.layer_toolbars if layer_toolbar.is_active]
            [layer_toolbar.layer.triggerRepaint() for layer_toolbar in self.layer_toolbars if layer_toolbar.is_active]
        self.canvas.refreshAllLayers()

    def set_crs(self, crs):
        self.crs = crs
        self.update_render_layers()

    def update_render_layers(self):
        with block_signals_to(self):
            from ThRasE.core.editing import LayerToEdit
            # set the CRS of the canvas view
            if self.crs:
                # use the crs of thematic layer to edit
                self.canvas.setDestinationCrs(self.crs)
            else:
                # use the crs set in Qgis
                self.canvas.setDestinationCrs(iface.mapCanvas().mapSettings().destinationCrs())
            # get all valid activated layers
            valid_layers = [layer_toolbar.layer for layer_toolbar in self.layer_toolbars
                           if layer_toolbar.is_active and layer_toolbar.layer is not None
                           and layer_toolbar.layer.isValid()]
            if len(valid_layers) == 0:
                self.canvas.setLayers([])
                self.refresh()
                return
            # include registry memory layer if active
            if LayerToEdit.current and LayerToEdit.current.registry.memory_layer:
                memory_layer = LayerToEdit.current.registry.memory_layer
                # add registry layer on top of other layers
                self.canvas.setLayers([memory_layer] + valid_layers)
            else:
                # set to canvas without registry layer
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
