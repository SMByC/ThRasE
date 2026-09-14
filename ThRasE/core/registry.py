"""
/***************************************************************************
 ThRasE

 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2026 by Xavier Corredor Llano, SMByC
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

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsFillSymbol,
    QgsGeometry,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor

#: Number of pixels one global edit may add to the registry before ThRasE asks the
#: user to confirm.  An internal guard, not a user setting: the registry has no size
#: limit, and the dialogs never show this number.
#:
#: Measured on this code, each recorded pixel costs about 2.5 KB of memory (its
#: PixelLog and Pixel plus one polygon feature in the registry layer) and the cost
#: grows linearly: recording takes about 30 s per million pixels and every save of
#: the session serialises the whole registry again at about 11 s per million.  At
#: this threshold one edit adds roughly 650 MB, seven seconds to record, and three
#: seconds to each save, which is where it starts to be worth a question.
WARNING_REGISTRY_LIMIT = 250_000


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

        # build the feature for the layer; whoever inserts it sets feature_id
        geom = QgsGeometry.fromRect(self.extent)
        feature = QgsFeature(memory_layer.fields())
        feature.setGeometry(geom)
        feature.setAttributes([self.idx, self.group_idx, center_x, center_y])

        self.feature = feature
        self.feature_id = None


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
                center_x - width / 2, center_y - height / 2, center_x + width / 2, center_y + height / 2
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
        self.memory_layer = self._new_memory_layer()

    def _new_memory_layer(self):
        crs = self.layer_to_edit.qgs_layer.crs()
        memory_layer = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "ThRasE Registry", "memory")

        # add fields
        provider = memory_layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("tile_idx", QVariant.Int),
                QgsField("group_idx", QVariant.Int),
                QgsField("center_x", QVariant.Double),
                QgsField("center_y", QVariant.Double),
            ]
        )
        memory_layer.updateFields()

        # set initial state (hidden by default)
        memory_layer.setSubsetString("FALSE")  # hide all initially
        return memory_layer

    def setup_renderer(self, color=None):
        """Setup single renderer for draw the registry tiles.

        Args:
            color: QColor to use for border. If None, uses the default color
        """
        if color is None:
            color = self.tiles_color

        # Simple border renderer - no fill, 0.3 border width
        border_symbol = QgsFillSymbol.createSimple(
            {
                "color": "transparent",
                "style": "no",
                "outline_color": color.name(),
                "outline_width": "0.3",
                "outline_style": "solid",
            }
        )
        self.renderer = QgsSingleSymbolRenderer(border_symbol)

    def update_registry_layer_in_canvases(self):
        """Refresh render layers in all active canvases to update registry layer visibility."""
        from ThRasE.gui.main_dialog import ThRasEDialog

        for view_widget in ThRasEDialog.view_widgets:
            if view_widget.is_active:
                view_widget.render_widget.update_render_layers()

    def delete(self):
        previous_layer = self.memory_layer
        previous_groups = self.groups
        previous_current_group = self.current_group
        self.groups = []
        self.current_group = None
        self.memory_layer = self._new_memory_layer()
        try:
            self.update_registry_layer_in_canvases()
        except Exception as error:
            self.memory_layer = previous_layer
            self.groups = previous_groups
            self.current_group = previous_current_group
            try:
                self.update_registry_layer_in_canvases()
            except Exception as restore_error:
                raise RuntimeError(f"Unable to restore the previous registry layer: {restore_error}") from error
            raise

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

        registry_visible = (
            ThRasE.dialog
            and ThRasE.dialog.registry_widget
            and ThRasE.dialog.registry_widget.isVisible()
            and self.enabled
            and self.current_group
        )

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

    def update(self, force_rebuild=False):
        """Update registry state after pixel edits.

        One interactive edit adds exactly one new group, and that case only
        appends features to the existing layer. Every other case rebuilds the
        layer and swaps it in atomically, which is correct but costs one feature
        per registered pixel.
        """
        grouped_logs = {}
        for pixel_log in self.layer_to_edit.pixel_log_store.values():
            if pixel_log.group_id is not None:
                grouped_logs.setdefault(pixel_log.group_id, []).append(pixel_log)

        if not grouped_logs:
            if self.groups:
                self.delete()
            return False

        existing_groups = {group.group_id: group for group in self.groups}
        new_ids = set(grouped_logs) - set(existing_groups)
        removed_ids = set(existing_groups) - set(grouped_logs)
        # An existing group can only lose pixels, because re-editing a pixel moves it
        # to the group of the newer edit. Equal counts therefore mean equal membership.
        membership_changed = any(
            len(existing_groups[group_id].tiles) != len(grouped_logs[group_id])
            for group_id in set(grouped_logs) & set(existing_groups)
        )
        if force_rebuild or removed_ids or membership_changed or not self.groups:
            return self.add_registry_groups(grouped_logs)
        if not new_ids:
            return bool(self.groups)
        return self.append_registry_groups(grouped_logs, new_ids)

    def append_registry_groups(self, group_id_to_logs, new_ids):
        """Add whole new groups to the existing layer without rebuilding it.

        Falls back to the atomic rebuild whenever appending cannot keep the
        layer consistent, so the layer never holds features that no group owns.
        """
        entries = []
        for group_id in new_ids:
            logs = group_id_to_logs[group_id]
            if not logs:
                continue
            logs_sorted = sorted(logs, key=lambda pl: pl.edit_date)
            entries.append((group_id, logs_sorted[0].edit_date, logs_sorted))
        if not entries:
            return bool(self.groups)
        entries.sort(key=lambda item: item[1])

        # Group indices must stay in chronological order for the review slider.
        if entries[0][1] < max(group.edit_date for group in self.groups):
            return self.add_registry_groups(group_id_to_logs)

        psx = self.layer_to_edit.qgs_layer.rasterUnitsPerPixelX()
        psy = self.layer_to_edit.qgs_layer.rasterUnitsPerPixelY()
        next_idx = max(group.idx for group in self.groups)
        tiles_to_add = []
        groups_to_add = []
        for group_id, first_date, logs_sorted in entries:
            next_idx += 1
            tiles = [
                RegistryTile(idx, next_idx, pl.pixel.x(), pl.pixel.y(), psx, psy, self.memory_layer)
                for idx, pl in enumerate(logs_sorted, start=1)
            ]
            tiles_to_add.extend(tiles)
            groups_to_add.append(RegistryTileGroup(next_idx, group_id, first_date, tiles, self.memory_layer, self))

        provider = self.memory_layer.dataProvider()
        success, added_features = provider.addFeatures([tile.feature for tile in tiles_to_add])
        if not success or len(added_features) != len(tiles_to_add):
            # Rebuilding replaces the layer, so partially inserted features cannot linger.
            return self.add_registry_groups(group_id_to_logs)
        for tile, feature in zip(tiles_to_add, added_features, strict=True):
            tile.feature_id = feature.id()
        self.memory_layer.updateExtents()

        previous_groups = self.groups
        previous_current_group = self.current_group
        self.groups = [*self.groups, *groups_to_add]
        if self.current_group is None:
            self.current_group = self.groups[0]
        try:
            self.update_registry_layer_in_canvases()
            self.memory_layer.triggerRepaint()
        except Exception:
            # Take the appended features back out so the groups always describe
            # exactly what the layer holds.
            self.groups = previous_groups
            self.current_group = previous_current_group
            provider.deleteFeatures([tile.feature_id for tile in tiles_to_add])
            raise
        return True

    def add_registry_groups(self, group_id_to_logs):
        """Build a replacement registry layer and swap it in only after success."""
        entries = []
        for gid, logs in group_id_to_logs.items():
            if not logs:
                continue
            logs_sorted = sorted(logs, key=lambda pl: pl.edit_date)
            first_date = logs_sorted[0].edit_date
            entries.append((gid, first_date, logs_sorted))

        if not entries:
            return False

        entries.sort(key=lambda item: item[1])

        target_layer = self._new_memory_layer()
        psx = self.layer_to_edit.qgs_layer.rasterUnitsPerPixelX()
        psy = self.layer_to_edit.qgs_layer.rasterUnitsPerPixelY()

        tiles_to_add = []
        groups_to_add = []
        for next_idx, (gid, fdate, logs_sorted) in enumerate(entries, start=1):
            tiles = []
            for idx, pl in enumerate(logs_sorted, start=1):
                cx = pl.pixel.x()
                cy = pl.pixel.y()
                tile = RegistryTile(idx, next_idx, cx, cy, psx, psy, target_layer)
                tiles.append(tile)
                tiles_to_add.append(tile)
            groups_to_add.append(RegistryTileGroup(next_idx, gid, fdate, tiles, target_layer, self))

        provider = target_layer.dataProvider()
        success, added_features = provider.addFeatures([tile.feature for tile in tiles_to_add])
        if not success or len(added_features) != len(tiles_to_add):
            raise RuntimeError("Unable to write the replacement pixel registry layer")
        for tile, feature in zip(tiles_to_add, added_features, strict=True):
            tile.feature_id = feature.id()
        target_layer.updateExtents()

        previous_layer = self.memory_layer
        previous_groups = self.groups
        previous_current_group = self.current_group
        previous_current_id = previous_current_group.group_id if previous_current_group else None
        previous_subset = previous_layer.subsetString() if previous_layer is not None else "FALSE"
        self.memory_layer = target_layer
        self.groups = groups_to_add
        self.current_group = next(
            (group for group in self.groups if group.group_id == previous_current_id),
            self.groups[0] if self.groups else None,
        )
        try:
            if previous_subset == "":
                self.memory_layer.setSubsetString("")
            elif previous_subset.startswith('"group_idx"') and self.current_group is not None:
                self.memory_layer.setSubsetString(f'"group_idx" = {self.current_group.idx}')
            else:
                self.memory_layer.setSubsetString("FALSE")
            if self.memory_layer.subsetString() != "FALSE":
                self.memory_layer.setRenderer(self.renderer.clone())
            self.update_registry_layer_in_canvases()
            self.memory_layer.triggerRepaint()
        except Exception as error:
            self.memory_layer = previous_layer
            self.groups = previous_groups
            self.current_group = previous_current_group
            try:
                self.update_registry_layer_in_canvases()
            except Exception as restore_error:
                raise RuntimeError(f"Unable to restore the previous registry layer: {restore_error}") from error
            raise

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
        values = (
            int(value)
            for log in self.layer_to_edit.pixel_log_store.values()
            for value in (log.old_value, log.new_value)
        )
        # GPKG supports signed int64. Text preserves UInt64 and values which other
        # formats/readers would coerce through double precision.
        value_type_name = "LongLong"
        limit = (1 << 63) - 1 if ext == ".gpkg" else (1 << 53) - 1
        if any(value < -(1 << 63) or value > limit or (ext != ".gpkg" and value < -limit) for value in values):
            value_type_name = "String"
        value_type = getattr(QVariant, value_type_name)
        convert_value = str if value_type_name == "String" else int
        fields.append(QgsField("group_id", QVariant.Int))
        fields.append(QgsField("old_value", value_type))
        fields.append(QgsField("new_value", value_type))
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
            feat.setAttributes(
                [
                    group_ids.get(pl.group_id),
                    convert_value(pl.old_value),
                    convert_value(pl.new_value),
                    pl.edit_date.isoformat(),
                ]
            )
            features.append(feat)

        if not features:
            return False, "No features to export", 0

        # write to disk
        crs = self.layer_to_edit.qgs_layer.crs()
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver_name
        options.fileEncoding = "UTF-8"
        options.layerName = os.path.splitext(os.path.basename(output_file_path))[0]
        options.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile

        writer = QgsVectorFileWriter.create(
            output_file_path, fields, QgsWkbTypes.Type.Polygon, crs, QgsCoordinateTransformContext(), options
        )
        if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
            return False, f"Error creating file: {writer.errorMessage()}", 0

        try:
            success = writer.addFeatures(features)  # Batch write instead of one-by-one
            if not success:
                return False, f"Error writing features: {writer.errorMessage()}", 0
        except Exception as e:
            return False, f"Error writing features: {e!s}", 0
        finally:
            del writer

        return True, "Pixel registry exported", len(features)
