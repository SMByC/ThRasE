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
from math import ceil

from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsRubberBand
from qgis.core import QgsRectangle, QgsPointXY, QgsGeometry, QgsWkbTypes


class Tile(object):
    def __init__(self, n_tile, xmin, xmax, ymin, ymax):
        self.num = n_tile
        self.geom = None  # square
        self.extent = QgsRectangle(xmin, ymin, xmax, ymax)

        from ThRasE.core.edition import LayerToEdit
        self.polygon = QgsRubberBand(LayerToEdit.current.build_navigation_dialog.render_widget.canvas)
        points = [QgsPointXY(xmin, ymax), QgsPointXY(xmax, ymax),
                  QgsPointXY(xmax, ymin), QgsPointXY(xmin, ymin)]
        self.polygon.setToGeometry(QgsGeometry.fromPolygonXY([points]), None)
        self.polygon.setColor(QColor(0, 0, 255))
        self.polygon.setFillColor(QColor(0, 0, 0, 0))
        self.polygon.setWidth(1)

        self.show()

    def focus(self):
        from ThRasE.core.edition import LayerToEdit
        [tile.hide() for tile in LayerToEdit.current.navigation.tiles]
        self.show()
        LayerToEdit.current.build_navigation_dialog.render_widget.canvas.setExtent(self.extent)

    def show(self):
        self.polygon.show()

    def hide(self):
        self.polygon.hide()

    def is_valid(self):
        pass


class Navigation(object):

    def __init__(self, layer_to_edit):
        self.layer_to_edit = layer_to_edit
        self.navigation_is_valid = False
        self.current_tile = None
        self.tiles = []

    def build_navigation(self, tile_size, nav_mode):
        self.clean()

        rectangle_nav = self.layer_to_edit.qgs_layer.extent()

        nx_tiles = ceil((rectangle_nav.xMaximum() - rectangle_nav.xMinimum()) / tile_size)
        ny_tiles = ceil((rectangle_nav.yMaximum() - rectangle_nav.yMinimum()) / tile_size)

        idx_tile = 0
        # left-right and top-bottom
        if nav_mode == "horizontal":
            x_left_right = []
            for y_tile in range(ny_tiles):
                for x_tile in range(nx_tiles):
                    if y_tile % 2 == 0:  # left to right
                        xmin = rectangle_nav.xMinimum() + x_tile * tile_size
                        xmax = xmin + tile_size
                        x_left_right.append((xmin, xmax))
                    else:  # right to left
                        xmin, xmax = x_left_right.pop()
                    ymax = rectangle_nav.yMaximum() - y_tile * tile_size
                    ymin = ymax - tile_size

                    # fix/adjust right and bottom tiles borders
                    if xmax > rectangle_nav.xMaximum():
                        xmax = rectangle_nav.xMaximum()
                    if ymin < rectangle_nav.yMinimum():
                        ymin = rectangle_nav.yMinimum()

                    tile = Tile(idx_tile, xmin, xmax, ymin, ymax)
                    if tile.is_valid():
                        self.tiles.append(tile)
                        idx_tile += 1
        # top-bottom and left-right
        if nav_mode == "vertical":
            y_top_bottom = []
            for x_tile in range(nx_tiles):
                for y_tile in range(ny_tiles):
                    if x_tile % 2 == 0:  # top to bottom
                        ymax = rectangle_nav.yMaximum() - y_tile * tile_size
                        ymin = ymax - tile_size
                        y_top_bottom.append((ymin, ymax))
                    else:  # bottom to top
                        ymin, ymax = y_top_bottom.pop()
                    xmin = rectangle_nav.xMinimum() + x_tile * tile_size
                    xmax = xmin + tile_size

                    # fix/adjust right and bottom tiles borders
                    if xmax > rectangle_nav.xMaximum():
                        xmax = rectangle_nav.xMaximum()
                    if ymin < rectangle_nav.yMinimum():
                        ymin = rectangle_nav.yMinimum()

                    tile = Tile(idx_tile, xmin, xmax, ymin, ymax)
                    if tile.is_valid():
                        self.tiles.append(tile)
                        idx_tile += 1

        # init
        self.navigation_is_valid = True
        self.current_tile = next((tile for tile in self.tiles if tile.num == 0))

    def clean(self):

        for tile in self.tiles:
            tile.polygon.reset(QgsWkbTypes.PolygonGeometry)

        self.navigation_is_valid = False
        self.current_tile = None
        self.tiles = []