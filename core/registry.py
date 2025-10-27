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

import os

from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsRectangle,
    QgsGeometry,
    QgsWkbTypes,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsVectorFileWriter,
    QgsCoordinateTransformContext,
    QgsVectorLayer,
    QgsSingleSymbolRenderer,
    QgsFillSymbol,
)

from ThRasE.utils.system_utils import wait_process


class RegistryTile:
    def __init__(self, idx, group_idx, center_x, center_y, px_size_x, px_size_y, memory_layer):
        self.idx = idx
        self.group_idx = group_idx
        self.center_x = center_x
        self.center_y = center_y
        self.px_size_x = px_size_x
        self.px_size_y = px_size_y

        xmin = center_x - px_size_x / 2
        xmax = center_x + px_size_x / 2
        ymin = center_y - px_size_y / 2
        ymax = center_y + px_size_y / 2

        self.extent = QgsRectangle(xmin, ymin, xmax, ymax)

        # create feature in memory layer
        geom = QgsGeometry.fromRect(self.extent)
        feature = QgsFeature(memory_layer.fields())
        feature.setGeometry(geom)
        feature.setAttributes([self.idx, self.group_idx, center_x, center_y])
        
        # add feature to memory layer
        memory_layer.dataProvider().addFeature(feature)
        self.feature_id = feature.id()


class RegistryTileGroup:
    def __init__(self, idx, group_id, edit_date, tiles, memory_layer, registry):
        self.idx = idx
        self.group_id = group_id
        self.edit_date = edit_date
        self.tiles = tiles
        self.memory_layer = memory_layer
        self.registry = registry
        self.extent = None

    def tiles_extent(self):
        if self.extent is None:
            extent = QgsRectangle()
            for tile in self.tiles:
                extent.combineExtentWith(tile.extent)
            self.extent = extent
        return self.extent

    def show(self):
        # display tile borders without fill by applying filter to memory layer
        if not self.tiles:
            return
        
        # set filter to show only tiles from this group
        filter_expr = f'"group_idx" = {self.idx}'
        self.memory_layer.setSubsetString(filter_expr)
        self.memory_layer.triggerRepaint()
        self.registry.refresh_all_canvases()

    def clear(self):
        # hide tiles by setting a filter that matches nothing
        self.memory_layer.setSubsetString("FALSE")
        self.memory_layer.triggerRepaint()
        self.registry.refresh_all_canvases()

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
        self.enabled = True
        
        # create memory vector layer for tiles
        self.memory_layer = None
        self.create_memory_layer()
        
        # single renderer for both display modes
        self.renderer = None
        self.setup_renderer()

    def create_memory_layer(self):
        """Create memory vector layer to store tile geometries."""
        crs = self.layer_to_edit.qgs_layer.crs()
        self.memory_layer = QgsVectorLayer(
            f"Polygon?crs={crs.authid()}", 
            "ThRasE Registry", 
            "memory"
        )
        
        # add fields
        provider = self.memory_layer.dataProvider()
        provider.addAttributes([
            QgsField("tile_idx", QVariant.Int),
            QgsField("group_idx", QVariant.Int),
            QgsField("center_x", QVariant.Double),
            QgsField("center_y", QVariant.Double),
        ])
        self.memory_layer.updateFields()
        
        # set initial state (hidden by default)
        self.memory_layer.setSubsetString("FALSE")  # hide all initially

    def setup_renderer(self, color=None):
        """Setup single renderer for draw the registry tiles.
        
        Args:
            color: QColor to use for border. If None, uses the default color
        """
        if color is None:
            color = self.tiles_color
        
        # Simple border renderer - no fill, 0.3 border width
        border_symbol = QgsFillSymbol.createSimple({
            'color': 'transparent',
            'style': 'no',
            'outline_color': color.name(),
            'outline_width': '0.3',
            'outline_style': 'solid'
        })
        self.renderer = QgsSingleSymbolRenderer(border_symbol)
    
    def update_registry_layer_in_canvases(self):
        """Refresh render layers in all active canvases to update registry layer visibility."""
        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            if view_widget.is_active:
                view_widget.render_widget.update_render_layers()

    def delete(self):
        self.clear()
        self.groups = []
        self.current_group = None
        
        # clear memory layer
        if self.memory_layer:
            self.memory_layer.dataProvider().truncate()
            self.update_registry_layer_in_canvases()

    def clear(self):
        # hide all features by setting a filter that matches nothing
        if self.memory_layer:
            self.memory_layer.setSubsetString("FALSE")
            self.memory_layer.triggerRepaint()
    
    def refresh_all_canvases(self):
        """Refresh all active view widget canvases."""
        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            if view_widget.is_active:
                view_widget.render_widget.canvas.refresh()

    def show_all(self):
        """Display all tiles with border."""
        if not self.groups or not self.memory_layer:
            return
        
        # ensure layer is in canvases
        self.update_registry_layer_in_canvases()
        
        # apply renderer
        self.memory_layer.setRenderer(self.renderer.clone())
        
        # show all features (remove filter)
        self.memory_layer.setSubsetString("")
        self.memory_layer.triggerRepaint()
        self.refresh_all_canvases()

    def clear_show_all(self):
        """Clear the show all display and restore current group if registry is active."""
        if not self.memory_layer:
            return

        # check if registry widget is visible and enabled, and if we have a current group
        from ThRasE.thrase import ThRasE
        registry_visible = (ThRasE.dialog and 
                           ThRasE.dialog.registry_widget and 
                           ThRasE.dialog.registry_widget.isVisible() and 
                           self.enabled and 
                           self.current_group)
        
        if registry_visible:
            # restore current group display
            self.memory_layer.setRenderer(self.renderer.clone())
            filter_expr = f'"group_idx" = {self.current_group.idx}'
            self.memory_layer.setSubsetString(filter_expr)
        else:
            # hide all features
            self.memory_layer.setSubsetString("FALSE")
        
        self.memory_layer.triggerRepaint()
        self.refresh_all_canvases()

    def update_color(self):
        """Update the border color for current display."""
        # recreate renderer with current color
        self.setup_renderer(self.tiles_color)
        
        # if currently displaying something, update renderer
        if self.memory_layer and self.memory_layer.subsetString() != "FALSE":
            self.memory_layer.setRenderer(self.renderer.clone())
            self.memory_layer.triggerRepaint()
            self.refresh_all_canvases()

    def update(self):
        # clear previous
        self.delete()
        
        # recreate memory layer
        self.create_memory_layer()
        self.setup_renderer()

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

        # start editing mode for batch feature addition
        self.memory_layer.startEditing()

        idx_group = 1
        for gid, fdate, logs_sorted in grouped:
            tiles = []
            for idx, pl in enumerate(logs_sorted, start=1):
                cx = pl.pixel.x()
                cy = pl.pixel.y()
                tiles.append(RegistryTile(idx, idx_group, cx, cy, psx, psy, self.memory_layer))
            self.groups.append(RegistryTileGroup(idx_group, gid, fdate, tiles, self.memory_layer, self))
            idx_group += 1

        # commit changes to memory layer
        self.memory_layer.commitChanges()
        self.memory_layer.updateExtents()

        # init current group
        self.current_group = self.groups[0] if self.groups else None
        if self.current_group is None:
            return False

        return True

    def set_current_group(self, idx_group):
        from ThRasE.thrase import ThRasE
        
        self.current_group = next((g for g in self.groups if g.idx == idx_group), None)
        if not self.current_group:
            return
        
        # ensure layer is in canvases
        self.update_registry_layer_in_canvases()
        
        # apply renderer for current group
        if self.memory_layer:
            self.memory_layer.setRenderer(self.renderer.clone())
        
        if ThRasE.dialog.registry_widget.autoCenter.isChecked():
            self.current_group.center()
        self.current_group.show()

    def export_registry(self, output_file_path):
        """Export all registered pixel edits (registry) to a vector file.

        Each feature is a square polygon representing the pixel modified and
        includes attributes: group_id, old_value, new_value, edit_date.

        Returns (success: bool, message: str, written_count: int).
        """
        if not self.layer_to_edit.pixel_log_store:
            return False, "No features to export", 0

        # ensure output extension and driver
        path, ext = os.path.splitext(output_file_path)
        ext = ext.lower()
        if ext not in [".gpkg", ".shp", ".geojson"]:
            output_file_path = path + ".gpkg"
            ext = ".gpkg"
        
        driver_name = {".gpkg": "GPKG", ".shp": "ESRI Shapefile", ".geojson": "GeoJSON"}.get(ext, "GPKG")

        # create mapping from UUID group_id
        group_ids = {group.group_id: group.idx for group in self.groups} if self.groups else {}

        # define fields
        fields = QgsFields()
        fields.append(QgsField("group_id", QVariant.Int))
        fields.append(QgsField("old_value", QVariant.Int))
        fields.append(QgsField("new_value", QVariant.Int))
        fields.append(QgsField("edit_date", QVariant.String))

        # pre-calculate pixel size (constant for all features)
        psx = self.layer_to_edit.qgs_layer.rasterUnitsPerPixelX()
        psy = self.layer_to_edit.qgs_layer.rasterUnitsPerPixelY()
        half_psx = psx / 2
        half_psy = psy / 2

        # build features efficiently
        features = []
        for pl in self.layer_to_edit.pixel_log_store.values():
            cx = pl.pixel.x()
            cy = pl.pixel.y()
            
            # create geometry
            rect = QgsRectangle(cx - half_psx, cy - half_psy, cx + half_psx, cy + half_psy)
            geom = QgsGeometry.fromRect(rect)

            # create feature
            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            feat.setAttributes([
                group_ids.get(pl.group_id),
                int(pl.old_value),
                int(pl.new_value),
                pl.edit_date.isoformat()
            ])
            features.append(feat)

        if not features:
            return False, "No features to export", 0

        # write to disk
        crs = self.layer_to_edit.qgs_layer.crs()
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver_name
        options.fileEncoding = "UTF-8"
        options.layerName = os.path.splitext(os.path.basename(output_file_path))[0]
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile

        writer = QgsVectorFileWriter.create(
            output_file_path, fields, QgsWkbTypes.Polygon, crs, QgsCoordinateTransformContext(), options
        )
        if writer.hasError() != QgsVectorFileWriter.NoError:
            return False, f"Error creating file: {writer.errorMessage()}", 0

        try:
            success = writer.addFeatures(features)  # Batch write instead of one-by-one
            if not success:
                return False, f"Error writing features: {writer.errorMessage()}", 0
        except Exception as e:
            return False, f"Error writing features: {str(e)}", 0
        finally:
            del writer

        return True, "Pixel registry exported", len(features)
