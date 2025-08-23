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
from math import ceil

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsRubberBand
from qgis.core import QgsRectangle, QgsPointXY, QgsGeometry, QgsWkbTypes

from ThRasE.utils.system_utils import wait_process


class NavigationTile(object):
    def __init__(self, idx, xmin, xmax, ymin, ymax, tile_color):
        self.idx = idx  # index order number of the tile, start in 1
        self.rbs_in_main_dialog = []  # rubber bands instances in the main dialog
        self.rbs_in_nav_dialog = []  # rubber bands instances in the navigation dialog
        self.extent = QgsRectangle(xmin, ymin, xmax, ymax)
        self.xmin, self.xmax, self.ymin, self.ymax = xmin, xmax, ymin, ymax
        self.tile_color = tile_color

    def create(self, canvas, line_width=2, rbs_in="main_dialog", current_idx_tile=None):
        """Create the tile as a rubber band inside the canvas given"""
        rubber_band = QgsRubberBand(canvas)
        points = [QgsPointXY(self.xmin, self.ymax), QgsPointXY(self.xmax, self.ymax),
                  QgsPointXY(self.xmax, self.ymin), QgsPointXY(self.xmin, self.ymin)]
        rubber_band.setToGeometry(QgsGeometry.fromPolygonXY([points]), None)
        if rbs_in == "highlight":
            rubber_band.setStrokeColor(QColor("yellow"))
        else:
            rubber_band.setStrokeColor(self.tile_color)

        if current_idx_tile is not None and self.idx < current_idx_tile:
            # fill color for tiles already reviewed in the navigation dialog
            fill_color = QColor(self.tile_color)
            fill_color.setAlpha(80)
        else:
            fill_color = QColor(0, 0, 0, 0)

        rubber_band.setFillColor(fill_color)
        rubber_band.setWidth(line_width)
        rubber_band.show()
        if rbs_in == "main_dialog":
            self.rbs_in_main_dialog.append(rubber_band)
        if rbs_in == "nav_dialog":
            self.rbs_in_nav_dialog.append(rubber_band)
        if rbs_in == "highlight":
            return rubber_band

    def show(self):
        """Show/draw the tile in all view widgets in main dialog"""
        self.hide()

        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            # for all view widgets in main dialog
            if view_widget.is_active:
                self.create(view_widget.render_widget.canvas)

    def hide(self):
        [rubber_band.reset() for rubber_band in self.rbs_in_main_dialog]

    def focus(self):
        """Adjust to the tile extent in all view widgets in main dialog"""
        # focus to extent with a bit of buffer
        from ThRasE.gui.main_dialog import ThRasEDialog
        buffer = (self.ymax - self.ymin) * 0.01
        extent_with_buffer = QgsRectangle(self.xmin-buffer, self.ymin-buffer, self.xmax+buffer, self.ymax+buffer)
        [view_widget.render_widget.update_canvas_to(extent_with_buffer) for view_widget in ThRasEDialog.view_widgets
         if view_widget.is_active]

        self.show()

        from ThRasE.thrase import ThRasE
        if not ThRasE.dialog.currentTileKeepVisible.isChecked():
            QTimer.singleShot(1200, self.hide)

    def is_valid(self, navigation):
        if navigation.nav_type in ["whole", "points"]:
            # all tiles are valid
            return True
        if navigation.nav_type == "polygons":
            # only the tiles that intersects the polygons are valid
            if True in [polygon.intersects(self.extent) for polygon in navigation.polygons]:
                return True
            else:
                return False


class Navigation(object):

    def __init__(self, layer_to_edit):
        self.layer_to_edit = layer_to_edit
        self.is_valid = False
        self.nav_type = None
        self.current_tile = None
        self.tiles = []
        self.tiles_color = QColor("#00aaff")

    @wait_process
    def build_navigation(self, tile_size, nav_mode, polygons=None, points=None):
        # define type of navigation
        if polygons:
            self.nav_type = "polygons"
            self.polygons = polygons
        elif points:
            self.nav_type = "points"
            self.points = points
        else:
            self.nav_type = "whole"

        # clear before build
        self.delete()

        # compute the tiles in whole image or covering the polygons
        if self.nav_type in ["whole", "polygons"]:
            # define the extent to compute the tiles
            if self.nav_type == "whole":
                rectangle_nav = self.layer_to_edit.qgs_layer.extent()
            if self.nav_type == "polygons":
                # compute the wrapper extent of all polygons
                rectangle_nav = QgsRectangle()
                for polygon in self.polygons:
                    rectangle_nav.combineExtentWith(polygon.boundingBox())
                # intersect with the layer to edit
                rectangle_nav = rectangle_nav.intersect(self.layer_to_edit.extent())

            # number of tiles
            nx_tiles = ceil((rectangle_nav.xMaximum() - rectangle_nav.xMinimum()) / tile_size)
            ny_tiles = ceil((rectangle_nav.yMaximum() - rectangle_nav.yMinimum()) / tile_size)

            idx_tile = 1
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

                        tile = NavigationTile(idx_tile, xmin, xmax, ymin, ymax, self.tiles_color)
                        if tile.is_valid(self):
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

                        tile = NavigationTile(idx_tile, xmin, xmax, ymin, ymax, self.tiles_color)
                        if tile.is_valid(self):
                            self.tiles.append(tile)
                            idx_tile += 1
        # compute the tiles using the points as center of the tiles
        if self.nav_type == "points":
            idx_tile = 1
            # left-right and top-bottom
            if nav_mode == "horizontal":
                points_sorted = sorted(self.points, key=lambda p: (-p.y(), p.x()))

            # top-bottom and left-right
            if nav_mode == "vertical":
                points_sorted = sorted(self.points, key=lambda p: (p.x(), -p.y()))

            for point in points_sorted:
                # check if point is inside layer to edit
                if not self.layer_to_edit.extent().contains(point):
                    continue
                xmin = point.x() - tile_size/2
                xmax = point.x() + tile_size/2
                ymin = point.y() - tile_size/2
                ymax = point.y() + tile_size/2
                tile = NavigationTile(idx_tile, xmin, xmax, ymin, ymax, self.tiles_color)
                if tile.is_valid(self):
                    self.tiles.append(tile)
                    idx_tile += 1

        # init
        self.current_tile = next((tile for tile in self.tiles if tile.idx == 1), None)

        # show all tiles in build navigation canvas dialog
        from ThRasE.core.editing import LayerToEdit
        [tile.create(LayerToEdit.current.navigation_dialog.render_widget.canvas, rbs_in="nav_dialog",
                     current_idx_tile=self.current_tile.idx) for tile in self.tiles]

        return True if self.current_tile is not None else False

    def set_current_tile(self, idx_tile):
        self.clear(rbs_in="main_dialog")
        self.current_tile = next((tile for tile in self.tiles if tile.idx == idx_tile), None)
        self.current_tile.show()
        self.current_tile.focus()

        # update the review tiles in the navigation dialog
        self.clear(rbs_in="nav_dialog")
        from ThRasE.core.editing import LayerToEdit
        [tile.create(LayerToEdit.current.navigation_dialog.render_widget.canvas, rbs_in="nav_dialog",
                     current_idx_tile=idx_tile) for tile in self.tiles]

    def clear(self, rbs_in="main_dialog"):
        """Clear all tiles drawn (rubber bands instances)"""
        if rbs_in == "main_dialog":
            for tile in self.tiles:
                for rubber_band in tile.rbs_in_main_dialog:
                    rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                tile.rbs_in_main_dialog = []
        if rbs_in == "nav_dialog":
            for tile in self.tiles:
                for rubber_band in tile.rbs_in_nav_dialog:
                    rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                tile.rbs_in_nav_dialog = []

    def delete(self):
        from ThRasE.thrase import ThRasE
        from ThRasE.core.editing import LayerToEdit

        self.clear(rbs_in="main_dialog")
        self.clear(rbs_in="nav_dialog")
        if LayerToEdit.current.navigation_dialog.highlight_tile:
            LayerToEdit.current.navigation_dialog.highlight_tile.reset(QgsWkbTypes.PolygonGeometry)
        self.is_valid = False
        self.current_tile = None
        self.tiles = []

        # disable navigations widgets
        LayerToEdit.current.navigation_dialog.SliderNavigationBlock.setEnabled(False)
        ThRasE.dialog.NavigationBlockWidgetControls.setEnabled(False)
