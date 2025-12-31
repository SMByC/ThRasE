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
            # set the CRS of the canvas view
            if self.crs:
                self.canvas.setDestinationCrs(self.crs)
            else:
                self.canvas.setDestinationCrs(iface.mapCanvas().mapSettings().destinationCrs())
            
            # get all valid activated layers
            valid_layers = [layer_toolbar.layer for layer_toolbar in self.layer_toolbars
                           if layer_toolbar.is_active and layer_toolbar.layer is not None
                           and layer_toolbar.layer.isValid()]
            
            if not valid_layers:
                self.canvas.setLayers([])
                self.refresh()
                return
            
            # include registry memory layer if exists
            from ThRasE.core.editing import LayerToEdit
            memory_layer = (LayerToEdit.current.registry.memory_layer 
                           if LayerToEdit.current and LayerToEdit.current.registry.memory_layer 
                           else None)
            
            layers_to_set = [memory_layer] + valid_layers if memory_layer else valid_layers
            self.canvas.setLayers(layers_to_set)
            
            # set init extent from other view if any is activated else set layer extent
            from ThRasE.gui.main_dialog import ThRasEDialog
            others_extents = [view_widget.render_widget.canvas.extent() 
                             for view_widget in ThRasEDialog.view_widgets
                             if view_widget.is_active and view_widget.render_widget != self
                             and not view_widget.render_widget.canvas.extent().isEmpty()]
            
            if others_extents:
                self.update_canvas_to(others_extents[0])
            elif self.canvas.extent().isEmpty():
                # first layer to render - set extent using the first valid layer
                transform = QgsCoordinateTransform(
                    valid_layers[0].crs(), 
                    self.canvas.mapSettings().destinationCrs(), 
                    QgsProject.instance()
                )
                new_extent = transform.transformBoundingBox(valid_layers[0].extent())
                self.canvas.setExtent(new_extent)

            self.refresh()

    def update_canvas_to(self, new_extent):
        with block_signals_to(self.canvas):
            self.canvas.setExtent(new_extent)
            self.refresh()
