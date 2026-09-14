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

import functools
import math
import os
import tempfile
import uuid
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

import numpy as np
import yaml

try:
    from yaml import CSafeDumper as SafeDumper
except ImportError:
    from yaml import SafeDumper

import itertools

from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsRasterBlock
from qgis.PyQt.QtCore import Qt

from ThRasE.core.navigation import Navigation
from ThRasE.core.raster_recode import (
    RasterRecodeRecoveryError,
    RecodeRequest,
    RecodeStatus,
    discard_staged,
    raster_integer_limits,
    stage_recode,
    validate_integer_value,
)
from ThRasE.core.registry import Registry
from ThRasE.utils.others_utils import get_xml_style
from ThRasE.utils.qgis_utils import apply_symbology, commit_and_reconcile, get_source_from, session_layer_source
from ThRasE.utils.system_utils import block_signals_to, wait_process

if TYPE_CHECKING:
    from ThRasE.gui.navigation_dialog import NavigationDialog


def check_before_editing():
    from ThRasE.thrase import ThRasE

    # check if the recode pixel table is empty
    if LayerToEdit.current is None or not LayerToEdit.current.old_new_value:
        ThRasE.dialog.MsgBar.pushMessage(
            "There are no changes to apply in the recode pixel table. Please set new pixel values first",
            level=Qgis.MessageLevel.Warning,
            duration=10,
        )
        return False
    return True


def edit_layer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from ThRasE.thrase import ThRasE

        target = args[0] if args and isinstance(args[0], LayerToEdit) else LayerToEdit.current
        if target is None or target is not LayerToEdit.current:
            return None
        # Validate the entire mapping before the first write of a multi-pixel edit.
        # This goes through validate_new_value, which reads the band's limits once per
        # target: the pixel picker edits on every mouse move, so reading them here would
        # reopen the raster for each painted pixel.
        if args and isinstance(args[0], LayerToEdit):
            for value in target.old_new_value.values():
                target.validate_new_value(value)
        provider = target.data_provider
        was_editable = provider.isEditable()
        if not was_editable:
            if not provider.setEditable(True):
                ThRasE.dialog.MsgBar.pushMessage(
                    "The current thematic raster cannot be edited due to layer restrictions or permission issues",
                    level=Qgis.MessageLevel.Critical,
                    duration=20,
                )
                return None
        try:
            return func(*args, **kwargs)
        finally:
            if not was_editable:
                provider.setEditable(False)

    return wrapper


class LayerToEdit:
    instances: ClassVar[dict] = {}
    current = None

    def __init__(self, layer, band):
        self.qgs_layer = layer
        self.data_provider = layer.dataProvider()
        self.file_path = get_source_from(layer)
        self.band = band
        self.bounds = layer.extent().toRectF().getCoords()  # (xmin , ymin, xmax, ymax)
        # navigation
        self.navigation = Navigation(self)
        # Created only when navigation is explicitly enabled
        self.navigation_dialog: NavigationDialog | None = None
        # store pixels: value, color, new_value, on/off, label
        #   -> [{"value": int, "color": {"R", "G", "B", "A"}, "new_value": int, "s/h": bool, "label": str}, ...]
        self.pixels_backup = None  # backup for save the original values
        self.pixels = None
        # user personalization of value-color table of class pixels
        #   -> [("name", value, (R, G, B, A)), ...]
        self.symbology = None
        # dictionary for quick search the new value based on the old value in the recode table
        self.old_new_value = {}
        self._integer_limits = None
        # setup decimal-place tolerance for comparing pixels, derived from pixel size
        pixel_size = min(self.qgs_layer.rasterUnitsPerPixelX(), self.qgs_layer.rasterUnitsPerPixelY())
        self.pixel_tolerance = 1 - math.floor(math.log10(abs(pixel_size))) + (1 if abs(pixel_size) >= 1 else 0)
        # store the PixelLog store specific to this layer instance
        self.pixel_log_store = {}
        # registry of edits
        self.registry = Registry(self)
        # nodata handling: "unset", "hide", or None (not yet decided)
        self.nodata_action = None
        # save config file
        self.config_file = None

        LayerToEdit.instances[(layer.id(), band)] = self

    def extent(self):
        return self.qgs_layer.extent()

    def get_pixel_value_from_xy(self, x, y):
        return self.get_pixel_value_from_pnt(QgsPointXY(x, y))

    def get_pixel_value_from_pnt(self, point):
        if not self.check_point_inside_layer(point):
            return None
        result = self.data_provider.identify(point, Qgis.RasterIdentifyFormat.Value)
        return result.results().get(self.band) if result.isValid() else None

    def setup_pixel_table(self, force_update=False, nodata=None):
        if self.pixels is None or force_update is True:
            xml_style_items = get_xml_style(self.qgs_layer, self.band)
            if xml_style_items is None:
                self.pixels = None
                return False

            self.pixels = []
            for xml_item in xml_style_items:
                if nodata is not None and int(xml_item.get("value")) == int(nodata):
                    continue

                pixel = {
                    "value": int(xml_item.get("value")),
                    "color": {},
                    "new_value": None,
                    "s/h": True,
                    "label": xml_item.get("label", ""),
                }

                item_color = xml_item.get("color").lstrip("#")
                item_color = tuple(int(item_color[i : i + 2], 16) for i in (0, 2, 4))
                pixel["color"]["R"] = item_color[0]
                pixel["color"]["G"] = item_color[1]
                pixel["color"]["B"] = item_color[2]
                pixel["color"]["A"] = int(xml_item.get("alpha"))

                # for pixels style that come with transparency
                if pixel["color"]["A"] < 255:
                    pixel["color"]["A"] = 255
                    pixel["s/h"] = False

                self.pixels.append(pixel)

            # save backup
            if not self.pixels_backup:
                self.pixels_backup = deepcopy(self.pixels)
            # init the symbology table
            self.setup_symbology()

    def setup_symbology(self):
        # fill/restart the symbology based on the real pixel-color values from file
        self.symbology = [
            (
                pixel.get("label") or str(pixel["value"]),
                pixel["value"],
                (pixel["color"]["R"], pixel["color"]["G"], pixel["color"]["B"], 255 if pixel["s/h"] else 0),
            )
            for pixel in self.pixels
        ]

        apply_symbology(self.qgs_layer, self.band, self.symbology)

    def get_old_and_new_pixel_values(self, pixel):
        old_value = self.get_pixel_value_from_pnt(pixel.qgs_point)
        return old_value, self.old_new_value[old_value] if old_value in self.old_new_value and self.old_new_value[
            old_value
        ] != old_value else None

    def highlight_value_in_recode_pixel_table(self, value_to_select):
        """Highlight the current pixel value from mouse pointer on canvas"""
        from ThRasE.thrase import ThRasE

        if self.pixels is None:
            return
        if value_to_select is None:
            ThRasE.dialog.recodePixelTable.clearSelection()
            with block_signals_to(ThRasE.dialog.recodePixelTable):
                [
                    ThRasE.dialog.recodePixelTable.item(idx, 2).setBackground(Qt.GlobalColor.white)
                    for idx in range(len(self.pixels))
                ]
            return

        row_idx = next((idx for idx, i in enumerate(self.pixels) if i["value"] == value_to_select), None)
        if row_idx is not None:
            # select
            ThRasE.dialog.recodePixelTable.setCurrentCell(row_idx, 2)
            # set background
            with block_signals_to(ThRasE.dialog.recodePixelTable):
                [
                    ThRasE.dialog.recodePixelTable.item(idx, 2).setBackground(Qt.GlobalColor.white)
                    for idx in range(len(self.pixels))
                ]
                ThRasE.dialog.recodePixelTable.item(row_idx, 2).setBackground(Qt.GlobalColor.yellow)
        else:
            ThRasE.dialog.recodePixelTable.clearSelection()
            with block_signals_to(ThRasE.dialog.recodePixelTable):
                [
                    ThRasE.dialog.recodePixelTable.item(idx, 2).setBackground(Qt.GlobalColor.white)
                    for idx in range(len(self.pixels))
                ]

    def check_point_inside_layer(self, pixel):
        # check if the pixel is within active raster bounds
        return bool(self.bounds[0] <= pixel.x() < self.bounds[2] and self.bounds[1] < pixel.y() <= self.bounds[3])

    def validate_new_value(self, value):
        """Validate against the band range, including NBITS, cached for this target."""
        if self._integer_limits is None:
            self._integer_limits = raster_integer_limits(self.file_path, self.band)
        validate_integer_value(value, self._integer_limits)

    def edit_pixel(self, pixel, new_value=None, group_id=None, store=None):
        if not self.check_point_inside_layer(pixel):
            return None
        if new_value is None:
            old_value, new_value = self.get_old_and_new_pixel_values(pixel)
            if new_value is None:
                return
        else:
            old_value = self.get_pixel_value_from_pnt(pixel.qgs_point)

        if old_value is None or not math.isfinite(old_value):
            return None
        self.validate_new_value(new_value)
        px = math.floor((pixel.x() - self.bounds[0]) / self.qgs_layer.rasterUnitsPerPixelX())
        py = math.floor((self.bounds[3] - pixel.y()) / self.qgs_layer.rasterUnitsPerPixelY())
        if not (0 <= px < self.qgs_layer.width() and 0 <= py < self.qgs_layer.height()):
            return None

        rblock = QgsRasterBlock(self.data_provider.dataType(self.band), 1, 1)
        rblock.setValue(0, 0, new_value)
        if int(rblock.value(0, 0)) != new_value:
            raise ValueError(f"Value {new_value} cannot be represented exactly by the raster provider")
        if self.data_provider.writeBlock(rblock, self.band, px, py):  # write and check if writing status is ok
            record = self.registry.enabled if store is None else store
            log = PixelLog(pixel, old_value, new_value, group_id, store=False)
            existing = self.pixel_log_store.get(pixel)
            if existing is not None:
                if existing.old_value == new_value:
                    del self.pixel_log_store[pixel]
                else:
                    existing.new_value = int(new_value)
                    if record:
                        existing.edit_date, existing.group_id = log.edit_date, group_id
            elif record:
                self.pixel_log_store[pixel] = log
            return log

    @wait_process
    @edit_layer
    def edit_from_pixel_picker(self, pixel):
        group_id = uuid.uuid4()
        pixel_log = self.edit_pixel(pixel, group_id=group_id)

        from ThRasE.thrase import ThRasE

        ThRasE.dialog.editing_status.setText(f"{1 if pixel_log else 0} pixel edited!")

        if pixel_log:  # the pixel was edited
            if hasattr(self.qgs_layer, "setCacheImage"):
                self.qgs_layer.setCacheImage(None)
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            ThRasE.dialog.registry_widget.update_registry()
            # pixel value edited to send to the history
            pixel_value = pixel_log.old_value
            return pixel_value

    @wait_process
    @edit_layer
    def edit_from_line_picker(self, line_feature, line_buffer):
        if line_feature is None:
            return

        ps_x = self.qgs_layer.rasterUnitsPerPixelX()  # pixel size in x
        ps_y = self.qgs_layer.rasterUnitsPerPixelY()  # pixel size in y
        ps_avg = (ps_x + ps_y) / 2  # average of the pixel size, when the pixel is not square

        # function for check if the pixel must be edited
        def check_pixel_to_edit_in(x, y):
            pc_x = self.bounds[0] + int((x - self.bounds[0]) / ps_x) * ps_x + ps_x / 2  # locate the pixel centroid in x
            pc_y = self.bounds[3] - int((self.bounds[3] - y) / ps_y) * ps_y - ps_y / 2  # locate the pixel centroid in y

            point = QgsPointXY(pc_x, pc_y)
            if line_feature.geometry().distance(QgsGeometry.fromPointXY(point)) <= ps_avg * line_buffer:
                # return the pixel-point to edit
                return point

        # analysis all pixel if is inside in the segments of box of pair pixel consecutive
        polyline = line_feature.geometry().asPolyline()
        points_to_process = [
            [
                check_pixel_to_edit_in(x, y)
                for y in np.arange(
                    min(p1.y(), p2.y()) - ps_y * line_buffer, max(p1.y(), p2.y()) + ps_y * line_buffer, ps_y
                )
                for x in np.arange(
                    min(p1.x(), p2.x()) - ps_x * line_buffer, max(p1.x(), p2.x()) + ps_x * line_buffer, ps_x
                )
            ]
            for p1, p2 in itertools.pairwise(polyline)
        ]
        # flat the list, clean None and duplicates
        pixels_to_process = [
            Pixel(point=point) for point in {item for sublist in points_to_process for item in sublist if item}
        ]

        # edit and return all the pixel and value before edit it, for save in history class
        group_id = uuid.uuid4()
        pixel_logs = [self.edit_pixel(pixel, group_id=group_id) for pixel in pixels_to_process]
        pixel_logs = [item for item in pixel_logs if item]  # clean None, unedited pixels

        from ThRasE.thrase import ThRasE

        ThRasE.dialog.editing_status.setText(f"{len(pixel_logs)} pixels edited!")

        if pixel_logs:
            if hasattr(self.qgs_layer, "setCacheImage"):
                self.qgs_layer.setCacheImage(None)
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            ThRasE.dialog.registry_widget.update_registry()
            # pixels and values edited to send to the history
            pixels_and_values = [(pixel_log.pixel, pixel_log.old_value) for pixel_log in pixel_logs]
            return pixels_and_values

    @wait_process
    @edit_layer
    def edit_from_polygon_picker(self, polygon_feature):
        if polygon_feature is None:
            return

        group_id = uuid.uuid4()
        pixel_logs = [
            log
            for pixel in self.pixels_in_geometry(polygon_feature.geometry())
            if (log := self.edit_pixel(pixel, group_id=group_id)) is not None
        ]

        from ThRasE.thrase import ThRasE

        ThRasE.dialog.editing_status.setText(f"{len(pixel_logs)} pixels edited!")

        if pixel_logs:
            if hasattr(self.qgs_layer, "setCacheImage"):
                self.qgs_layer.setCacheImage(None)
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            ThRasE.dialog.registry_widget.update_registry()
            # pixels and values edited to send to the history
            pixels_and_values = [(pixel_log.pixel, pixel_log.old_value) for pixel_log in pixel_logs]
            return pixels_and_values

    @wait_process
    @edit_layer
    def edit_from_freehand_picker(self, freehand_feature):
        if freehand_feature is None:
            return

        group_id = uuid.uuid4()
        pixel_logs = [
            log
            for pixel in self.pixels_in_geometry(freehand_feature.geometry())
            if (log := self.edit_pixel(pixel, group_id=group_id)) is not None
        ]

        from ThRasE.thrase import ThRasE

        ThRasE.dialog.editing_status.setText(f"{len(pixel_logs)} pixels edited!")

        if pixel_logs:
            if hasattr(self.qgs_layer, "setCacheImage"):
                self.qgs_layer.setCacheImage(None)
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            ThRasE.dialog.registry_widget.update_registry()
            # pixels and values edited to send to the history
            pixels_and_values = [(pixel_log.pixel, pixel_log.old_value) for pixel_log in pixel_logs]
            return pixels_and_values

    def pixels_in_geometry(self, geometry):
        """Yield selected centres lazily, clipping candidate indices to the raster."""
        box = geometry.boundingBox()
        xmin, _, _, ymax = self.bounds
        dx, dy = self.qgs_layer.rasterUnitsPerPixelX(), self.qgs_layer.rasterUnitsPerPixelY()
        col_start = max(0, math.floor((box.xMinimum() - xmin) / dx))
        col_stop = min(self.qgs_layer.width(), math.floor((box.xMaximum() - xmin) / dx) + 1)
        row_start = max(0, math.floor((ymax - box.yMaximum()) / dy))
        row_stop = min(self.qgs_layer.height(), math.floor((ymax - box.yMinimum()) / dy) + 1)
        engine = QgsGeometry.createGeometryEngine(geometry.constGet())
        engine.prepareGeometry()
        for row in range(row_start, row_stop):
            for column in range(col_start, col_stop):
                point = QgsPointXY(xmin + (column + 0.5) * dx, ymax - (row + 0.5) * dy)
                candidate = QgsGeometry.fromPointXY(point)
                if engine.contains(candidate.constGet()):
                    yield Pixel(point=point)

    @wait_process
    def edit_to_entire_thematic_raster(self, record_in_registry=False):
        """Synchronously recode the raster through the bounded-memory engine.

        The dialog runs global edits through the cancellable task controller.
        This method is the same sequence without a task, for callers that need it
        to finish before they continue; it records every changed pixel when asked
        to, without the size confirmation the dialogs show.
        """
        from ThRasE.thrase import ThRasE

        registry_pixels = tuple(self.pixel_log_store)
        try:
            request = RecodeRequest(
                source_path=self.file_path,
                band=self.band,
                recode_pairs=tuple(self.old_new_value.items()),
                collect_changes=record_in_registry,
                registry_points=tuple((pixel.x(), pixel.y()) for pixel in registry_pixels),
            )
            result = stage_recode(request)
        except Exception as error:
            # A failed staging leaves nothing behind, so there is nothing to discard.
            ThRasE.dialog.MsgBar.pushMessage(f"ERROR: {error}", level=Qgis.MessageLevel.Critical, duration=20)
            return False

        if result.status is RecodeStatus.NO_CHANGES:
            edited_pixels_count = 0
        else:
            try:
                outcome = commit_and_reconcile(
                    result,
                    self,
                    record_changes=record_in_registry,
                    registry_pixels=registry_pixels,
                    registry_widget=ThRasE.dialog.registry_widget,
                )
            except Exception as error:
                retained = discard_staged(result, keep_for_recovery=isinstance(error, RasterRecodeRecoveryError))
                if retained:
                    ThRasE.dialog.MsgBar.pushMessage(
                        "Global edit files remain beside the raster and block further global edits until they are "
                        "checked and removed: " + ", ".join(retained),
                        level=Qgis.MessageLevel.Warning,
                        duration=20,
                    )
                ThRasE.dialog.MsgBar.pushMessage(f"ERROR: {error}", level=Qgis.MessageLevel.Critical, duration=20)
                return False

            if outcome.recovery_error is not None:
                ThRasE.dialog.MsgBar.pushMessage(
                    f"ERROR: {outcome.recovery_error}", level=Qgis.MessageLevel.Critical, duration=20
                )
                return False
            if outcome.cleanup_error is not None:
                ThRasE.dialog.MsgBar.pushMessage(
                    "The global edit succeeded, but its temporary files could not be removed: "
                    f"{outcome.cleanup_error}. Remaining files: " + ", ".join(outcome.leftovers),
                    level=Qgis.MessageLevel.Warning,
                    duration=20,
                )
            if outcome.retained_backups:
                ThRasE.dialog.MsgBar.pushMessage(
                    "The original raster was kept because another program wrote to it after the global edit. Check "
                    "it and remove it manually to allow further global edits: " + ", ".join(outcome.retained_backups),
                    level=Qgis.MessageLevel.Warning,
                    duration=20,
                )
            if outcome.registry_error is not None:
                ThRasE.dialog.MsgBar.pushMessage(
                    f"The raster was edited, but the registry could not be updated: {outcome.registry_error}",
                    level=Qgis.MessageLevel.Warning,
                    duration=20,
                )
            edited_pixels_count = outcome.edited_count

        ThRasE.dialog.editing_status.setText(f"{edited_pixels_count} pixels edited!")

        return edited_pixels_count

    def store_global_edit_changes(self, changes, geotransform):
        """Store verified global-edit change records in one registry group."""
        group_id = uuid.uuid4()
        previous_store = self.pixel_log_store
        updated_store = previous_store.copy()
        for change in changes:
            x_coord = geotransform[0] + (change.column + 0.5) * geotransform[1] + (change.row + 0.5) * geotransform[2]
            y_coord = geotransform[3] + (change.column + 0.5) * geotransform[4] + (change.row + 0.5) * geotransform[5]
            pixel_log = PixelLog(
                Pixel(x=x_coord, y=y_coord),
                change.old_value,
                change.new_value,
                group_id,
                store=False,
            )
            existing = updated_store.get(pixel_log.pixel)
            if existing is None:
                updated_store[pixel_log.pixel] = pixel_log
            elif existing.old_value == pixel_log.new_value:
                del updated_store[pixel_log.pixel]
            else:
                updated_store[existing.pixel] = PixelLog(
                    existing.pixel,
                    existing.old_value,
                    pixel_log.new_value,
                    group_id,
                    edit_date=pixel_log.edit_date,
                    store=False,
                )
        self.pixel_log_store = updated_store
        try:
            registry_updated = self.registry.update()
            if updated_store and not registry_updated:
                raise RuntimeError("Unable to rebuild the pixel registry")
        except Exception:
            self.pixel_log_store = previous_store
            raise

    def reconcile_registry(self, current_values=None, pixels=None):
        """Keep existing registry entries consistent after an unlogged edit."""
        previous_store = self.pixel_log_store
        reconciled_store = {}
        if current_values is None:
            pixels = tuple(previous_store)
            current_values = tuple(self.get_pixel_value_from_pnt(pixel.qgs_point) for pixel in pixels)
        elif pixels is None or len(current_values) != len(pixels):
            raise RuntimeError("The registry reconciliation result does not match the requested pixels")
        for pixel, current_value in zip(pixels, current_values, strict=True):
            pixel_log = previous_store.get(pixel)
            if pixel_log is None:
                raise RuntimeError("The pixel registry changed before it could be reconciled")
            if current_value is None:
                reconciled_store[pixel] = pixel_log
                continue
            current_value = int(current_value)
            if current_value == pixel_log.old_value:
                continue
            if current_value == pixel_log.new_value:
                reconciled_store[pixel] = pixel_log
            else:
                reconciled_store[pixel] = PixelLog(
                    pixel,
                    pixel_log.old_value,
                    current_value,
                    pixel_log.group_id,
                    edit_date=pixel_log.edit_date,
                    store=False,
                )
        self.pixel_log_store = reconciled_store
        try:
            registry_updated = self.registry.update()
            if reconciled_store and not registry_updated:
                raise RuntimeError("Unable to rebuild the pixel registry")
        except Exception:
            self.pixel_log_store = previous_store
            raise

    @wait_process
    def save_config(self, file_out):
        """Save the session atomically; return True only after replacement succeeds."""
        from ThRasE.thrase import ThRasE

        def setup_yaml():
            """
            Return a dumper that preserves key order for mappings.
            """

            class OrderedDumper(SafeDumper):
                """Custom dumper that keeps insertion order for dict-like objects."""

                pass

            def represent_ordered_mapping(dumper, data):
                return dumper.represent_mapping(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, list(data.items()))

            OrderedDumper.add_representer(dict, represent_ordered_mapping)
            OrderedDumper.add_representer(OrderedDict, represent_ordered_mapping)
            return OrderedDumper

        def setup_path(_path):
            """
            Sets up the path by calculating the relative path of input path
            to the directory of where yaml file is being saving.
            """
            if _path is None:
                return None
            # Get the directory of the reference file
            reference_dir = os.path.dirname(os.path.abspath(file_out))
            try:
                relative_path = os.path.relpath(_path, start=reference_dir)
                # If the relative path stays within the reference directory
                if not relative_path.startswith("..") and not os.path.isabs(relative_path):
                    return relative_path
                else:
                    return _path
            except Exception:
                # If the paths cannot be related, return the original input path
                return _path

        dumper = setup_yaml()

        data = OrderedDict()
        # general settings
        data["thematic_file_to_edit"] = {
            "path": setup_path(self.file_path),
            "band": self.band,
            "nodata_action": self.nodata_action,
        }
        data["grid_view_widgets"] = {"columns": ThRasE.dialog.grid_columns, "rows": ThRasE.dialog.grid_rows}
        data["main_dialog_size"] = (ThRasE.dialog.size().width(), ThRasE.dialog.size().height())
        data["config_file"] = file_out
        # recode pixel table
        data["recode_pixel_table"] = self.pixels
        data["recode_pixel_table_backup"] = self.pixels_backup
        # the colors of thematic raster
        data["symbology"] = self.symbology
        # view_widgets, layer toolbars and edit tool
        data["layer_toolbars_enabled"] = ThRasE.dialog.QPBtn_LayerToolbars.isChecked()
        data["num_layer_toolbars_per_view"] = ThRasE.dialog.QCBox_NumLayerToolbars.currentText()
        data["editing_toolbars_enabled"] = ThRasE.dialog.QPBtn_EditingToolbars.isChecked()
        # save the extent in the views using a view with a valid layer (not empty)
        from ThRasE.gui.main_dialog import ThRasEDialog

        for view_widget in ThRasEDialog.view_widgets:
            if view_widget.is_active and not view_widget.render_widget.canvas.extent().isEmpty():
                data["extent"] = view_widget.render_widget.canvas.extent().toRectF().getCoords()
                break
        # view_widgets, layer and editing toolbars
        data["view_widgets"] = []
        for view_widget in ThRasEDialog.view_widgets:
            layer_toolbars = []
            for layer_toolbar in view_widget.layer_toolbars:
                layer_toolbars.append(
                    {
                        "is_active": layer_toolbar.OnOff_LayerToolbar.isChecked(),
                        "layer_name": layer_toolbar.layer.name() if layer_toolbar.layer else None,
                        "layer_path": session_layer_source(layer_toolbar.layer, file_out),
                        "layer_provider": layer_toolbar.layer.providerType() if layer_toolbar.layer else None,
                        "opacity": layer_toolbar.opacity,
                    }
                )
            data["view_widgets"].append(
                {
                    "layer_toolbars": layer_toolbars,
                    "mouse_pixel_value": view_widget.mousePixelValue2Table.isChecked(),
                    "pixels_picker_enabled": view_widget.PixelsPicker.isChecked(),
                    "lines_picker_enabled": view_widget.LinesPicker.isChecked(),
                    "line_buffer": view_widget.LineBuffer.currentText(),
                    "lines_color": view_widget.lines_color.name(),
                    "polygons_picker_enabled": view_widget.PolygonsPicker.isChecked(),
                    "polygons_color": view_widget.polygons_color.name(),
                    "freehand_picker_enabled": view_widget.FreehandPicker.isChecked(),
                    "freehand_color": view_widget.freehand_color.name(),
                    "auto_clear_enabled": view_widget.AutoClear.isChecked(),
                }
            )
        # navigation
        data["navigation"] = {}

        if (
            self.navigation_dialog is None
            or not ThRasE.dialog.QPBtn_EnableNavigation.isChecked()
            or not self.navigation.is_valid
        ):
            data["navigation"]["type"] = "free"
        else:
            data["navigation"]["type"] = self.navigation_dialog.QCBox_BuildNavType.currentText()
            data["navigation"]["tile_keep_visible"] = ThRasE.dialog.currentTileKeepVisible.isChecked()
            data["navigation"]["tile_size"] = self.navigation_dialog.tileSize.value()
            data["navigation"]["mode"] = (
                "horizontal" if self.navigation_dialog.nav_horizontal_mode.isChecked() else "vertical"
            )
            data["navigation"]["tiles_color"] = self.navigation.tiles_color.name()
            data["navigation"]["current_tile_id"] = self.navigation.current_tile.idx
            data["navigation"]["size_dialog"] = (
                self.navigation_dialog.size().width(),
                self.navigation_dialog.size().height(),
            )
            data["navigation"]["extent_dialog"] = (
                self.navigation_dialog.render_widget.canvas.extent().toRectF().getCoords()
            )
            data["navigation"]["build_tools"] = self.navigation_dialog.QPBtn_BuildNavigationTools.isChecked()
            # special type navigation
            if data["navigation"]["type"] == "AOIs":
                aois = [
                    next([[pl.x(), pl.y()] for pl in pls] for pls in aoi.asGeometry().asMultiPolygon()[0])
                    for aoi in self.navigation_dialog.aoi_drawn
                ]
                data["navigation"]["aois"] = aois
            if data["navigation"]["type"] in ["polygons", "points", "centroid of polygons"]:
                vector = self.navigation_dialog.QCBox_VectorFile.currentLayer()
                data["navigation"]["vector_file"] = session_layer_source(vector, file_out)
                data["navigation"]["vector_provider"] = vector.providerType() if vector else None

        # registry (widget state and pixel logs)
        rw = ThRasE.dialog.registry_widget
        data["registry"] = {
            "enabled": self.registry.enabled,
            "opened": rw.isVisible(),
            "tiles_color": self.registry.tiles_color.name(),
            "slider_position": int(rw.PixelLogGroups_Slider.value()),
            "auto_center": rw.autoCenter.isChecked(),
            "show_all": rw.showAll.isChecked(),
        }

        # serialize all pixel logs from the current layer registry
        def serialize_pixel_log(pixel_log):
            return {
                "x": pixel_log.pixel.x(),
                "y": pixel_log.pixel.y(),
                "old_value": int(pixel_log.old_value),
                "new_value": int(pixel_log.new_value),
                "edit_date": pixel_log.edit_date.isoformat(),
                "group_id": str(pixel_log.group_id) if pixel_log.group_id is not None else None,
            }

        # compress pixel logs: JSON -> gzip -> base64
        import base64
        import gzip
        import json

        pixel_logs = list(self.pixel_log_store.values())
        pixel_logs.sort(key=lambda pl: (pl.edit_date, str(pl.group_id)))
        pixel_logs_serialized = [serialize_pixel_log(pl) for pl in pixel_logs]
        json_bytes = json.dumps(pixel_logs_serialized, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        gz_bytes = gzip.compress(json_bytes)
        b64_str = base64.b64encode(gz_bytes).decode("ascii")
        data["registry"]["pixel_logs"] = b64_str
        data["registry"]["pixel_logs_encoding"] = "gzip+base64+json"
        data["registry"]["pixel_logs_count"] = len(pixel_logs_serialized)

        # CCD plugin config
        if ThRasE.dialog.ccd_plugin_available:
            from CCD_Plugin.utils.config import get_plugin_config

            data["ccd_plugin_config"] = get_plugin_config(ThRasE.dialog.ccd_plugin.id)
            data["ccd_plugin_opened"] = ThRasE.dialog.QPBtn_CCDPlugin.isChecked()

        # Serialize beside the destination so a failed write leaves the previous session intact.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=os.path.dirname(os.path.abspath(file_out)),
                prefix=".thrase-config-",
                delete=False,
            ) as yaml_file:
                temporary = yaml_file.name
                yaml.dump(data, yaml_file, Dumper=dumper, default_flow_style=False, sort_keys=False)
                yaml_file.flush()
                os.fsync(yaml_file.fileno())
            os.replace(temporary, file_out)
            temporary = None
            self.config_file = file_out
            return True
        finally:
            if temporary is not None:
                os.unlink(temporary)


class Pixel:
    def __eq__(self, other):
        if not isinstance(other, Pixel):
            return NotImplemented
        return self._key == other._key

    def __hash__(self):
        return hash(self._key)

    def __init__(self, x=None, y=None, point=None):
        self.qgs_point = QgsPointXY(point) if point is not None else QgsPointXY(x, y)
        tolerance = LayerToEdit.current.pixel_tolerance if LayerToEdit.current is not None else 12
        self._key = (round(self.qgs_point.x(), tolerance), round(self.qgs_point.y(), tolerance))

    def x(self):
        return self.qgs_point.x()

    def y(self):
        return self.qgs_point.y()

    def geometry(self):
        return QgsGeometry.fromPointXY(self.qgs_point)


class PixelLog:
    """Class for store the pixel changes"""

    def __eq__(self, other):
        return self.pixel == other.pixel

    def __hash__(self):
        return self.pixel.__hash__()

    def __init__(self, pixel, old_value, new_value, group_id, edit_date=None, store=True):
        self.pixel = pixel
        self.old_value = int(old_value)
        self.new_value = int(new_value)
        self.edit_date = edit_date or datetime.now()
        self.group_id = group_id

        if store:
            if self.pixel in LayerToEdit.current.pixel_log_store:
                # if the pixel is already registered, update it
                pixel_logged = LayerToEdit.current.pixel_log_store[self.pixel]
                if pixel_logged.old_value == self.new_value:
                    del LayerToEdit.current.pixel_log_store[self.pixel]
                else:
                    pixel_logged.new_value = self.new_value
                    pixel_logged.edit_date = self.edit_date
                    pixel_logged.group_id = self.group_id
            else:
                LayerToEdit.current.pixel_log_store[self.pixel] = self


class EditLog:
    """Undo/redo history for one view and one LayerToEdit instance.

    Replay is available only while this history's target is current.

    For pixels:
        [(Pixel, value), ...]

    for polygons:
        [(polygon_feature, ((Pixel, value), ...)), ...]

    """

    def __init__(self, edit_type):
        self.edit_type = edit_type
        self.target = LayerToEdit.current
        self.undos = []
        self.redos = []

    def can_be_undone(self):
        return self.target is LayerToEdit.current and len(self.undos) > 0

    def can_be_redone(self):
        return self.target is LayerToEdit.current and len(self.redos) > 0

    def get_current_status(self, edit_log_entry):
        if self.edit_type == "pixel":
            pixel, _ = edit_log_entry
            return pixel, LayerToEdit.current.get_pixel_value_from_pnt(pixel.qgs_point)
        if self.edit_type in ["line", "polygon"]:
            feature, pixel_values = edit_log_entry
            return feature, [
                (pixel, LayerToEdit.current.get_pixel_value_from_pnt(pixel.qgs_point)) for pixel, _ in pixel_values
            ]

    def undo(self):
        if self.can_be_undone():
            edit_log_entry = self.undos.pop()
            self.redos.append(self.get_current_status(edit_log_entry))
            return edit_log_entry

    def redo(self):
        if self.can_be_redone():
            edit_log_entry = self.redos.pop()
            self.undos.append(self.get_current_status(edit_log_entry))
            return edit_log_entry

    def add(self, edit_log_entry):
        if not self.undos and not self.redos:
            self.target = LayerToEdit.current
        if self.target is not LayerToEdit.current:
            raise ValueError("Cannot add history for a different raster or band")
        self.undos.append(edit_log_entry)
        self.redos = []
