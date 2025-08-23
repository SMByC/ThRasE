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

from qgis.PyQt.QtGui import QColor
from qgis.core import QgsRectangle, QgsGeometry, QgsWkbTypes
from qgis.gui import QgsRubberBand

from ThRasE.utils.system_utils import wait_process


class RegistryTile:
    def __init__(self, idx, center_x, center_y, px_size_x, px_size_y):
        self.idx = idx
        self.center_x = center_x
        self.center_y = center_y
        self.px_size_x = px_size_x
        self.px_size_y = px_size_y

        xmin = center_x - px_size_x / 2
        xmax = center_x + px_size_x / 2
        ymin = center_y - px_size_y / 2
        ymax = center_y + px_size_y / 2

        self.extent = QgsRectangle(xmin, ymin, xmax, ymax)

        # use fast rectangle geometry; polygon corners will be implicit
        self.geometry = QgsGeometry.fromRect(self.extent)


class RegistryTileGroup:
    def __init__(self, idx, group_id, edit_date, tiles, tile_color):
        self.idx = idx
        self.group_id = group_id
        self.edit_date = edit_date
        self.tiles = tiles
        self.tile_color = tile_color
        self.extent = None
        self.union_geom = None
        self.rubber_bands = []  # rubber bands for group outline

    def tiles_extent(self):
        if self.extent is None:
            extent = QgsRectangle()
            for tile in self.tiles:
                extent.combineExtentWith(tile.extent)
            self.extent = extent
        return self.extent

    @wait_process
    def show(self):
        # draw only the border outline of the group as a single polygon
        from ThRasE.gui.main_dialog import ThRasEDialog
        if not self.tiles:
            return
        
        # compute union geometry once and cache it
        if self.union_geom is None:
            if len(self.tiles) == 1:
                self.union_geom = self.tiles[0].geometry
            else:
                geoms = [tile.geometry for tile in self.tiles]
                self.union_geom = QgsGeometry.unaryUnion(geoms)
        union_geom = self.union_geom
        if union_geom is None or union_geom.isEmpty():
            return
        
        # avoid duplicate rubber bands if show() is called again
        if self.rubber_bands:
            self.clear()

        # draw the outline in all active view widgets
        for view_widget in ThRasEDialog.view_widgets:
            if not view_widget.is_active:
                continue
            # create rubber band for the group outline
            rb = QgsRubberBand(view_widget.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
            rb.setToGeometry(union_geom, None)
            rb.setStrokeColor(self.tile_color)
            rb.setFillColor(QColor(0, 0, 0, 0))  # transparent fill
            rb.setWidth(3)
            rb.show()
            self.rubber_bands.append(rb)
            view_widget.render_widget.canvas.clearCache()
            view_widget.render_widget.canvas.refresh()

    def clear(self):
        [rb.reset(QgsWkbTypes.PolygonGeometry) for rb in self.rubber_bands]
        self.rubber_bands = []

    def update_color(self, color=None):
        # update stroke color of existing rubber bands without rebuilding geometry
        if color is not None:
            self.tile_color = color
        if not self.rubber_bands:
            return
        for rb in self.rubber_bands:
            rb.setStrokeColor(self.tile_color)
            rb.setFillColor(QColor(0, 0, 0, 0))
            rb.setWidth(3)

    def center(self):
        # center the view on the group without changing zoom level
        from ThRasE.gui.main_dialog import ThRasEDialog
        tiles_extent = self.tiles_extent()
        if tiles_extent.isEmpty():
            return
        
        # get the center point of the group
        center_x = tiles_extent.center().x()
        center_y = tiles_extent.center().y()
        
        # center all active views on this point without changing scale
        for view_widget in ThRasEDialog.view_widgets:
            if not view_widget.is_active:
                continue
            current_extent = view_widget.render_widget.canvas.extent()
            # calculate new extent centered on the group but with same size
            width = current_extent.width()
            height = current_extent.height()
            new_extent = QgsRectangle(
                center_x - width/2, center_y - height/2,
                center_x + width/2, center_y + height/2
            )
            view_widget.render_widget.update_canvas_to(new_extent)


class Registry:
    def __init__(self, layer_to_edit):
        self.layer_to_edit = layer_to_edit
        self.groups = []
        self.current_group = None
        self.tiles_color = QColor("#ff00ff")
        # cache for show-all pixels modified geoms
        self.all_union_geom = None
        self.show_all_rubber_bands = []

    def delete(self):
        self.clear()
        self.clear_show_all()
        self.groups = []
        self.current_group = None
        self.all_union_geom = None

    def clear(self):
        for group in self.groups:
            group.clear()

    @wait_process
    def show_all(self):
        # draw all groups
        from ThRasE.gui.main_dialog import ThRasEDialog
        if not self.groups:
            return
        # avoid duplicates
        self.clear_show_all()
        # compute cached union of all groups if needed
        if self.all_union_geom is None:
            geoms = []
            for group in self.groups:
                # ensure each group's union geometry is computed once
                if group.union_geom is None:
                    if not group.tiles:
                        continue
                    if len(group.tiles) == 1:
                        group.union_geom = group.tiles[0].geometry
                    else:
                        gms = [t.geometry for t in group.tiles]
                        group.union_geom = QgsGeometry.unaryUnion(gms)
                if group.union_geom and not group.union_geom.isEmpty():
                    geoms.append(group.union_geom)
            if not geoms:
                return
            self.all_union_geom = QgsGeometry.unaryUnion(geoms)
        if self.all_union_geom is None or self.all_union_geom.isEmpty():
            return

        # draw all pixels groups modified as a single geometry per active view
        fill = QColor(self.tiles_color)
        fill.setAlpha(140)  # 55% transparent
        for view_widget in ThRasEDialog.view_widgets:
            if not view_widget.is_active:
                continue
            rb = QgsRubberBand(view_widget.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
            rb.setToGeometry(self.all_union_geom, None)
            rb.setFillColor(fill)
            rb.setStrokeColor(QColor(0, 0, 0, 0))
            rb.setWidth(0)
            rb.show()
            self.show_all_rubber_bands.append(rb)
            view_widget.render_widget.canvas.clearCache()
            view_widget.render_widget.canvas.refresh()

    def clear_show_all(self):
        [rb.reset(QgsWkbTypes.PolygonGeometry) for rb in self.show_all_rubber_bands]
        self.show_all_rubber_bands = []

    def update_show_all_color(self):
        # update the color of overlays without rebuilding geometry
        if not self.show_all_rubber_bands:
            return
        fill = QColor(self.tiles_color)
        fill.setAlpha(140)  # 55% transparent
        for rb in self.show_all_rubber_bands:
            rb.setFillColor(fill)
            rb.setStrokeColor(QColor(0, 0, 0, 0))
            rb.setWidth(0)

    def update_current_group_color(self):
        # update the color of the current group's outline without rebuilding geometry
        if not self.current_group:
            return
        self.current_group.update_color(self.tiles_color)

    def update(self):
        # clear previous
        self.delete()

        # group PixelLogs by group_id
        group_id_to_logs = {}
        for pixel_log in self.layer_to_edit.pixel_log_store.values():
            if pixel_log.group_id is None:
                # skip logs without group id
                continue
            group_id_to_logs.setdefault(pixel_log.group_id, []).append(pixel_log)

        if not group_id_to_logs:
            return False

        # sort groups by edit_date
        grouped = []
        for gid, logs in group_id_to_logs.items():
            # sort pixels inside by edit_date
            logs_sorted = sorted(logs, key=lambda pl: pl.edit_date)
            first_date = logs_sorted[0].edit_date
            grouped.append((gid, first_date, logs_sorted))

        grouped.sort(key=lambda item: item[1])

        # build RegistryTileGroup list
        psx = self.layer_to_edit.qgs_layer.rasterUnitsPerPixelX()
        psy = self.layer_to_edit.qgs_layer.rasterUnitsPerPixelY()

        idx_group = 1
        for gid, fdate, logs_sorted in grouped:
            tiles = []
            for idx, pl in enumerate(logs_sorted, start=1):
                cx = pl.pixel.x()
                cy = pl.pixel.y()
                tiles.append(RegistryTile(idx, cx, cy, psx, psy))
            self.groups.append(RegistryTileGroup(idx_group, gid, fdate, tiles, self.tiles_color))
            idx_group += 1

        # init current group
        self.current_group = self.groups[0] if self.groups else None
        if self.current_group is None:
            return False

        return True

    def set_current_group(self, idx_group):
        from ThRasE.thrase import ThRasE
        self.clear()
        self.current_group = next((g for g in self.groups if g.idx == idx_group), None)
        if not self.current_group:
            return
        if ThRasE.dialog.registry_widget.autoCenter.isChecked():
            self.current_group.center()
        self.current_group.show()
