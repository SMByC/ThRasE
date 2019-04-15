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
    def __init__(self, idx, xmin, xmax, ymin, ymax):
        self.idx = idx
        self.instances = []
        self.extent = QgsRectangle(xmin, ymin, xmax, ymax)
        self.xmin, self.xmax, self.ymin, self.ymax = xmin, xmax, ymin, ymax

    def create(self, canvas):
        """Create the tile as a rubber band inside the canvas given"""
        instance = QgsRubberBand(canvas)
        points = [QgsPointXY(self.xmin, self.ymax), QgsPointXY(self.xmax, self.ymax),
                  QgsPointXY(self.xmax, self.ymin), QgsPointXY(self.xmin, self.ymin)]
        instance.setToGeometry(QgsGeometry.fromPolygonXY([points]), None)
        instance.setColor(QColor(0, 0, 255))
        instance.setFillColor(QColor(0, 0, 0, 0))
        instance.setWidth(2)
        instance.show()
        self.instances.append(instance)

    def show(self):
        """Show/draw the tile in all view widgets in main dialog"""
        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            # for all view widgets in main dialog
            if view_widget.is_active:
                self.create(view_widget.render_widget.canvas)

    def focus(self):
        """Adjust to the tile extent in all view widgets in main dialog"""
        from ThRasE.gui.main_dialog import ThRasEDialog
        [view_widget.render_widget.update_canvas_to(self.extent) for view_widget in ThRasEDialog.view_widgets
         if view_widget.is_active]

    def is_valid(self):
        return True


class Navigation(object):

    def __init__(self, layer_to_edit):
        self.layer_to_edit = layer_to_edit
        self.is_valid = False
        self.current_tile = None
        self.tiles = []

    def build_navigation(self, tile_size, nav_mode):
        self.delete()

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

        # show all tiles in build navigation canvas dialog
        from ThRasE.core.edition import LayerToEdit
        [tile.create(LayerToEdit.current.build_navigation_dialog.render_widget.canvas) for tile in self.tiles]
        # init
        self.current_tile = next((tile for tile in self.tiles if tile.idx == 0), None)
        return True if self.current_tile is not None else False

    def previous_tile(self):
        self.clean()
        # set the previous tile
        self.current_tile = next((tile for tile in self.tiles if tile.idx == self.current_tile.idx - 1), None)
        self.current_tile.show()
        self.current_tile.focus()

    def next_tile(self):
        self.clean()
        # set the next tile
        self.current_tile = next((tile for tile in self.tiles if tile.idx == self.current_tile.idx + 1), None)
        self.current_tile.show()
        self.current_tile.focus()

    def clean(self):
        """Clean all tiles drawn (rubber bands instances)"""
        for tile in self.tiles:
            for instance in tile.instances:
                instance.reset(QgsWkbTypes.PolygonGeometry)
            tile.instances = []

    def delete(self):
        self.clean()
        self.is_valid = False
        self.current_tile = None
        self.tiles = []