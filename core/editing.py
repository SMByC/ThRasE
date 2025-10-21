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
import math
import functools
import uuid
import numpy as np
from datetime import datetime
from copy import deepcopy
from shutil import move
from osgeo import gdal
from collections import OrderedDict
import yaml
try:
    from yaml import CSafeDumper as SafeDumper
except ImportError:
    from yaml import SafeDumper

from qgis.core import QgsRaster, QgsPointXY, QgsRasterBlock, Qgis, QgsGeometry
from qgis.PyQt.QtCore import Qt

from ThRasE.core.navigation import Navigation
from ThRasE.core.registry import Registry
from ThRasE.utils.others_utils import get_xml_style
from ThRasE.utils.qgis_utils import get_file_path_of_layer, apply_symbology
from ThRasE.utils.system_utils import wait_process, block_signals_to


def check_before_editing():
    from ThRasE.thrase import ThRasE
    # check if the recode pixel table is empty
    if not LayerToEdit.current.old_new_value:
        ThRasE.dialog.MsgBar.pushMessage(
            "There are no changes to apply in the recode pixel table. Please set new pixel values first",
            level=Qgis.Warning, duration=10)
        return False
    return True


def edit_layer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from ThRasE.thrase import ThRasE
        # pre-edit cleanup: clear rubber bands of show all pixel changes for performance reasons
        ThRasE.dialog.registry_widget.showAll.setChecked(False)
        # set layer for edit
        if not LayerToEdit.current.data_provider.isEditable():
            if not LayerToEdit.current.data_provider.setEditable(True):
                from ThRasE.thrase import ThRasE
                ThRasE.dialog.MsgBar.pushMessage("The current thematic raster cannot be edited due to layer restrictions or permission issues",
                                                 level=Qgis.Critical, duration=20)
                return False
        # do
        obj_returned = func(*args, **kwargs)
        # close edition
        LayerToEdit.current.data_provider.setEditable(False)
        # finally return the object of func
        return obj_returned
    return wrapper


class LayerToEdit(object):
    instances = {}
    current = None

    def __init__(self, layer, band):
        self.qgs_layer = layer
        self.data_provider = layer.dataProvider()
        self.file_path = get_file_path_of_layer(layer)
        self.band = band
        self.bounds = layer.extent().toRectF().getCoords()  # (xmin , ymin, xmax, ymax)
        # navigation
        self.navigation = Navigation(self)
        self.navigation_dialog = None  # Created only when navigation is explicitly enabled
        # store pixels: value, color, new_value, on/off
        #   -> [{"value": int, "color": {"R", "G", "B", "A"}, "new_value": int, "s/h": bool}, ...]
        self.pixels_backup = None  # backup for save the original values
        self.pixels = None
        # user personalization of value-color table of class pixels
        #   -> [("name", value, (R, G, B, A)), ...]
        self.symbology = None
        # dictionary for quick search the new value based on the old value in the recode table
        self.old_new_value = {}
        # setup decimal-place tolerance for comparing pixels, derived from pixel size
        pixel_size = min(self.qgs_layer.rasterUnitsPerPixelX(), self.qgs_layer.rasterUnitsPerPixelY())
        self.pixel_tolerance = 1 - math.floor(math.log10(abs(pixel_size))) + (1 if abs(pixel_size) >= 1 else 0)
        # store the PixelLog store specific to this layer instance
        self.pixel_log_store = {}
        # registry of edits
        self.registry = Registry(self)
        # save config file
        self.config_file = None

        LayerToEdit.instances[(layer.id(), band)] = self

    def extent(self):
        return self.qgs_layer.extent()

    def get_pixel_value_from_xy(self, x, y):
        return self.data_provider.identify(QgsPointXY(x, y), QgsRaster.IdentifyFormatValue).results()[self.band]

    def get_pixel_value_from_pnt(self, point):
        return self.data_provider.identify(point, QgsRaster.IdentifyFormatValue).results()[self.band]

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

                pixel = {"value": int(xml_item.get("value")), "color": {}, "new_value": None, "s/h": True}

                item_color = xml_item.get("color").lstrip('#')
                item_color = tuple(int(item_color[i:i + 2], 16) for i in (0, 2, 4))
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
        self.symbology = \
            [(str(pixel["value"]), pixel["value"],
              (pixel["color"]["R"], pixel["color"]["G"], pixel["color"]["B"], 255 if pixel["s/h"] else 0))
             for pixel in self.pixels]

        apply_symbology(self.qgs_layer, self.band, self.symbology)

    def get_old_and_new_pixel_values(self, pixel):
        old_value = self.get_pixel_value_from_pnt(pixel.qgs_point)
        return old_value, self.old_new_value[old_value] \
            if old_value in self.old_new_value and self.old_new_value[old_value] != old_value else None

    def highlight_value_in_recode_pixel_table(self, value_to_select):
        """Highlight the current pixel value from mouse pointer on canvas"""
        from ThRasE.thrase import ThRasE
        if value_to_select is None:
            ThRasE.dialog.recodePixelTable.clearSelection()
            with block_signals_to(ThRasE.dialog.recodePixelTable):
                [ThRasE.dialog.recodePixelTable.item(idx, 2).setBackground(Qt.white) for idx in range(len(self.pixels))]
            return

        row_idx = next((idx for idx, i in enumerate(self.pixels) if i["value"] == value_to_select), None)
        if row_idx is not None:
            # select
            ThRasE.dialog.recodePixelTable.setCurrentCell(row_idx, 2)
            # set background
            with block_signals_to(ThRasE.dialog.recodePixelTable):
                [ThRasE.dialog.recodePixelTable.item(idx, 2).setBackground(Qt.white) for idx in range(len(self.pixels))]
                ThRasE.dialog.recodePixelTable.item(row_idx, 2).setBackground(Qt.yellow)
        else:
            ThRasE.dialog.recodePixelTable.clearSelection()
            with block_signals_to(ThRasE.dialog.recodePixelTable):
                [ThRasE.dialog.recodePixelTable.item(idx, 2).setBackground(Qt.white) for idx in range(len(self.pixels))]

    def check_point_inside_layer(self, pixel):
        # check if the pixel is within active raster bounds
        return True if self.bounds[0] <= pixel.x() <= self.bounds[2] \
                       and self.bounds[1] <= pixel.y() <= self.bounds[3] else False

    def edit_pixel(self, pixel, new_value=None, group_id=None, store=None):
        if new_value is None:
            old_value, new_value = self.get_old_and_new_pixel_values(pixel)
            if new_value is None:
                return
        else:
            old_value = self.get_pixel_value_from_pnt(pixel.qgs_point)

        px = int((pixel.x() - self.bounds[0]) / self.qgs_layer.rasterUnitsPerPixelX())  # num column position in x
        py = int((self.bounds[3] - pixel.y()) / self.qgs_layer.rasterUnitsPerPixelY())  # num row position in y

        rblock = QgsRasterBlock(self.data_provider.dataType(self.band), 1, 1)
        rblock.setValue(0, 0, new_value)
        if self.data_provider.writeBlock(rblock, self.band, px, py):  # write and check if writing status is ok
            return PixelLog(pixel, old_value, new_value, group_id, store=self.registry.enabled if store is None else store)

    @wait_process
    @edit_layer
    def edit_from_pixel_picker(self, pixel):
        group_id = uuid.uuid4()
        pixel_log = self.edit_pixel(pixel, group_id=group_id)

        from ThRasE.thrase import ThRasE
        ThRasE.dialog.editing_status.setText("{} pixel edited!".format(1 if pixel_log else 0))

        if pixel_log:  # the pixel was edited
            if hasattr(self.qgs_layer, 'setCacheImage'):
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
            pc_x = self.bounds[0] + int((x - self.bounds[0]) / ps_x)*ps_x + ps_x/2  # locate the pixel centroid in x
            pc_y = self.bounds[3] - int((self.bounds[3] - y) / ps_y)*ps_y - ps_y/2  # locate the pixel centroid in y

            point = QgsPointXY(pc_x, pc_y)
            if line_feature.geometry().distance(QgsGeometry.fromPointXY(point)) <= ps_avg*line_buffer:
                # return the pixel-point to edit
                return point

        # analysis all pixel if is inside in the segments of box of pair pixel consecutive
        polyline = line_feature.geometry().asPolyline()
        points_to_process = \
            [[check_pixel_to_edit_in(x, y)
              for y in np.arange(min(p1.y(), p2.y())-ps_y*line_buffer, max(p1.y(), p2.y())+ps_y*line_buffer, ps_y)
              for x in np.arange(min(p1.x(), p2.x())-ps_x*line_buffer, max(p1.x(), p2.x())+ps_x*line_buffer, ps_x)]
             for p1, p2 in zip(polyline[:-1], polyline[1:])]
        # flat the list, clean None and duplicates
        pixels_to_process = [Pixel(point=point) for point in set([item for sublist in points_to_process for item in sublist if item])]

        # edit and return all the pixel and value before edit it, for save in history class
        group_id = uuid.uuid4()
        pixel_logs = [self.edit_pixel(pixel, group_id=group_id) for pixel in pixels_to_process]
        pixel_logs = [item for item in pixel_logs if item]  # clean None, unedited pixels

        from ThRasE.thrase import ThRasE
        ThRasE.dialog.editing_status.setText("{} pixels edited!".format(len(pixel_logs)))

        if pixel_logs:
            if hasattr(self.qgs_layer, 'setCacheImage'):
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

        box = polygon_feature.geometry().boundingBox()
        ps_x = self.qgs_layer.rasterUnitsPerPixelX()  # pixel size in x
        ps_y = self.qgs_layer.rasterUnitsPerPixelY()  # pixel size in y

        # locate the pixel centroid in x and y for the pixel in min/max in the extent
        y_min = self.bounds[3] - int((self.bounds[3] - box.yMinimum()) / ps_y) * ps_y - ps_y / 2
        y_max = self.bounds[3] - int((self.bounds[3] - box.yMaximum()) / ps_y) * ps_y - ps_y / 2
        x_min = self.bounds[0] + int((box.xMinimum() - self.bounds[0]) / ps_x) * ps_x + ps_x / 2
        x_max = self.bounds[0] + int((box.xMaximum() - self.bounds[0]) / ps_x) * ps_x + ps_x / 2

        # create geometry engine for optimized contains operations
        geom_engine = QgsGeometry.createGeometryEngine(polygon_feature.geometry().constGet())
        geom_engine.prepareGeometry()
        points = \
            [QgsGeometry.fromPointXY(QgsPointXY(x, y))
             for y in np.arange(y_min, y_max + ps_y, ps_y)
             for x in np.arange(x_min, x_max + ps_x, ps_x)]
        points_inside_polygon = [p.asPoint() for p in points if geom_engine.contains(p.constGet())]

        group_id = uuid.uuid4()
        pixel_logs = [self.edit_pixel(Pixel(point=point), group_id=group_id) for point in points_inside_polygon]
        pixel_logs = [item for item in pixel_logs if item]  # clean None, unedited pixels

        from ThRasE.thrase import ThRasE
        ThRasE.dialog.editing_status.setText("{} pixels edited!".format(len(pixel_logs)))

        if pixel_logs:
            if hasattr(self.qgs_layer, 'setCacheImage'):
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

        box = freehand_feature.geometry().boundingBox()
        ps_x = self.qgs_layer.rasterUnitsPerPixelX()  # pixel size in x
        ps_y = self.qgs_layer.rasterUnitsPerPixelY()  # pixel size in y

        # locate the pixel centroid in x and y for the pixel in min/max in the extent
        y_min = self.bounds[3] - int((self.bounds[3] - box.yMinimum()) / ps_y) * ps_y - ps_y / 2
        y_max = self.bounds[3] - int((self.bounds[3] - box.yMaximum()) / ps_y) * ps_y - ps_y / 2
        x_min = self.bounds[0] + int((box.xMinimum() - self.bounds[0]) / ps_x) * ps_x + ps_x / 2
        x_max = self.bounds[0] + int((box.xMaximum() - self.bounds[0]) / ps_x) * ps_x + ps_x / 2

        # create geometry engine for optimized contains operations
        geom_engine = QgsGeometry.createGeometryEngine(freehand_feature.geometry().constGet())
        geom_engine.prepareGeometry()
        points = \
            [QgsGeometry.fromPointXY(QgsPointXY(x, y))
             for y in np.arange(y_min, y_max + ps_y, ps_y)
             for x in np.arange(x_min, x_max + ps_x, ps_x)]
        points_inside_freehand = [p.asPoint() for p in points if geom_engine.contains(p.constGet())]

        group_id = uuid.uuid4()
        pixel_logs = [self.edit_pixel(Pixel(point=point), group_id=group_id) for point in points_inside_freehand]
        pixel_logs = [item for item in pixel_logs if item]  # clean None, unedited pixels

        from ThRasE.thrase import ThRasE
        ThRasE.dialog.editing_status.setText("{} pixels edited!".format(len(pixel_logs)))

        if pixel_logs:
            if hasattr(self.qgs_layer, 'setCacheImage'):
                self.qgs_layer.setCacheImage(None)
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            ThRasE.dialog.registry_widget.update_registry()
            # pixels and values edited to send to the history
            pixels_and_values = [(pixel_log.pixel, pixel_log.old_value) for pixel_log in pixel_logs]
            return pixels_and_values

    @wait_process
    def edit_whole_image(self, record_in_registry=False):
        """Edit the whole image with the new values using gdal"""
        from ThRasE.thrase import ThRasE

        edited_pixels_count = 0
        row_indices = col_indices = None
        old_values = new_values_changed = None

        try:
            # read
            ds_in = gdal.Open(self.file_path, gdal.GA_ReadOnly)
            if ds_in is None:
                raise RuntimeError(f"Unable to open raster {self.file_path}")
            num_bands = ds_in.RasterCount
            src_band = ds_in.GetRasterBand(self.band)
            data_array = src_band.ReadAsArray().astype(int)
            new_data_array = deepcopy(data_array)

            # apply changes
            for old_value, new_value in self.old_new_value.items():
                new_data_array[data_array == old_value] = new_value

            # compute which pixels actually changed
            row_indices, col_indices = np.nonzero(new_data_array != data_array)
            edited_pixels_count = int(row_indices.size)
            if edited_pixels_count:
                old_values = data_array[row_indices, col_indices]
                new_values_changed = new_data_array[row_indices, col_indices]

            def safe_call(method, *args):
                try:
                    method(*args)
                except Exception:
                    pass

            def copy_band_metadata(src, dst):
                nodata = src.GetNoDataValue()
                if nodata is not None:
                    safe_call(dst.SetNoDataValue, nodata)

                color_table = src.GetRasterColorTable()
                if color_table is not None:
                    safe_call(dst.SetRasterColorTable, color_table.Clone())

                category_names = src.GetCategoryNames()
                if category_names:
                    safe_call(dst.SetCategoryNames, category_names)

                rat = src.GetDefaultRAT()
                if rat is not None:
                    safe_call(dst.SetDefaultRAT, rat.Clone())

                metadata_domains = src.GetMetadataDomainList()
                if not metadata_domains:
                    metadata = src.GetMetadata()
                    if metadata:
                        safe_call(dst.SetMetadata, metadata)
                else:
                    for domain in metadata_domains:
                        metadata = src.GetMetadata(domain)
                        if metadata:
                            safe_call(dst.SetMetadata, metadata, domain)

                unit = src.GetUnitType()
                if unit:
                    safe_call(dst.SetUnitType, unit)

                scale = src.GetScale()
                if scale is not None:
                    safe_call(dst.SetScale, scale)

                offset = src.GetOffset()
                if offset is not None:
                    safe_call(dst.SetOffset, offset)

                description = src.GetDescription()
                if description:
                    safe_call(dst.SetDescription, description)

                safe_call(dst.SetColorInterpretation, src.GetColorInterpretation())

            def copy_dataset_metadata(src, dst):
                metadata_domains = src.GetMetadataDomainList()
                if not metadata_domains:
                    metadata = src.GetMetadata()
                    if metadata:
                        safe_call(dst.SetMetadata, metadata)
                else:
                    for domain in metadata_domains:
                        metadata = src.GetMetadata(domain)
                        if metadata:
                            safe_call(dst.SetMetadata, metadata, domain)

                description = src.GetDescription()
                if description:
                    safe_call(dst.SetDescription, description)

                gcps = src.GetGCPs()
                if gcps:
                    safe_call(dst.SetGCPs, gcps, src.GetGCPProjection())

            # create file
            fn, ext = os.path.splitext(self.file_path)
            fn_out = fn + "_tmp" + ext
            driver_name = ds_in.GetDriver().ShortName
            driver = gdal.GetDriverByName(driver_name)
            if driver is None:
                raise RuntimeError(f"GDAL driver '{driver_name}' is not available")

            (x, y) = new_data_array.shape
            ds_out = None
            create_copy_used = False
            if driver.GetMetadataItem("DCAP_CREATECOPY") == "YES":
                ds_out = driver.CreateCopy(fn_out, ds_in)
                if ds_out is not None:
                    create_copy_used = True

            if ds_out is None:
                ds_out = driver.Create(fn_out, y, x, num_bands, src_band.DataType)
                if ds_out is None:
                    raise RuntimeError(f"Failed to create output raster {fn_out}")

            src_band_i = dst_band_i = None
            for band_index in range(1, num_bands + 1):
                src_band_i = ds_in.GetRasterBand(band_index)
                dst_band_i = ds_out.GetRasterBand(band_index)
                if band_index == self.band:
                    dst_band_i.WriteArray(new_data_array)
                elif not create_copy_used:
                    dst_band_i.WriteArray(src_band_i.ReadAsArray())
                copy_band_metadata(src_band_i, dst_band_i)
            del src_band_i, dst_band_i

            ds_out.SetGeoTransform(ds_in.GetGeoTransform())
            ds_out.SetProjection(ds_in.GetProjection())
            copy_dataset_metadata(ds_in, ds_out)

            ds_out.FlushCache()
            del ds_out, driver, src_band, ds_in
            move(fn_out, self.file_path)

            # record the changes in ThRasE registry
            if record_in_registry and edited_pixels_count:
                # enable the registry if not enabled
                if not LayerToEdit.current.registry.enabled:
                    ThRasE.dialog.registry_widget.EnableRegistry.setChecked(True)

                ps_x = self.qgs_layer.rasterUnitsPerPixelX()
                ps_y = self.qgs_layer.rasterUnitsPerPixelY()
                xmin, ymin, xmax, ymax = self.bounds

                group_id = uuid.uuid4()
                for row_idx, col_idx, old_val, new_val in zip(row_indices, col_indices, old_values, new_values_changed):
                    x_coord = xmin + (float(col_idx) + 0.5) * ps_x
                    y_coord = ymax - (float(row_idx) + 0.5) * ps_y
                    PixelLog(Pixel(x=x_coord, y=y_coord), int(old_val), int(new_val), group_id, store=True)

            del new_data_array, data_array
            if old_values is not None:
                del old_values, new_values_changed
            if row_indices is not None:
                del row_indices, col_indices
        except Exception as e:
            ThRasE.dialog.MsgBar.pushMessage(f"ERROR: {e}", level=Qgis.Critical, duration=20)
            return False

        if hasattr(self.qgs_layer, 'setCacheImage'):
            self.qgs_layer.setCacheImage(None)
        self.qgs_layer.reload()
        self.qgs_layer.triggerRepaint()

        ThRasE.dialog.editing_status.setText(f"{edited_pixels_count} pixels edited!")
        if record_in_registry and edited_pixels_count:
            ThRasE.dialog.registry_widget.update_registry()

        return edited_pixels_count

    @wait_process
    def save_config(self, file_out):
        from ThRasE.thrase import ThRasE
        # save in class
        self.config_file = file_out

        def setup_yaml():
            """
            Return a dumper that preserves key order for mappings.
            """

            class OrderedDumper(SafeDumper):
                """Custom dumper that keeps insertion order for dict-like objects."""
                pass

            def represent_ordered_mapping(dumper, data):
                return dumper.represent_mapping(
                    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                    list(data.items()))

            OrderedDumper.add_representer(dict, represent_ordered_mapping)
            OrderedDumper.add_representer(OrderedDict, represent_ordered_mapping)
            return OrderedDumper

        def setup_path(_path):
            """
            Sets up the path by calculating the relative path of input path to the directory of where yaml file is being saving.
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
            except:
                # If the paths cannot be related, return the original input path
                return _path

        dumper = setup_yaml()

        data = OrderedDict()
        # general settings
        data["thematic_file_to_edit"] = \
            {"path": setup_path(self.file_path),
             "band": self.band}
        data["grid_view_widgets"] = {"columns": ThRasE.dialog.grid_columns, "rows": ThRasE.dialog.grid_rows}
        data["main_dialog_size"] = (ThRasE.dialog.size().width(), ThRasE.dialog.size().height())
        data["config_file"] = self.config_file
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
                layer_toolbars.append({"is_active": layer_toolbar.OnOff_LayerToolbar.isChecked(),
                                      "layer_name": layer_toolbar.layer.name() if layer_toolbar.layer else None,
                                      "layer_path": setup_path(get_file_path_of_layer(layer_toolbar.layer)),
                                      "opacity": layer_toolbar.opacity})
            data["view_widgets"].append({"layer_toolbars": layer_toolbars,
                                         "mouse_pixel_value": view_widget.mousePixelValue2Table.isChecked(),
                                         "pixels_picker_enabled": view_widget.PixelsPicker.isChecked(),
                                         "lines_picker_enabled": view_widget.LinesPicker.isChecked(),
                                         "line_buffer": view_widget.LineBuffer.currentText(),
                                         "lines_color": view_widget.lines_color.name(),
                                         "polygons_picker_enabled": view_widget.PolygonsPicker.isChecked(),
                                         "polygons_color": view_widget.polygons_color.name(),
                                         "freehand_picker_enabled": view_widget.FreehandPicker.isChecked(),
                                         "freehand_color": view_widget.freehand_color.name(),
                                         "auto_clear_enabled": view_widget.AutoClear.isChecked()})
        # navigation
        data["navigation"] = {}

        if self.navigation_dialog is None or not ThRasE.dialog.QPBtn_EnableNavigation.isChecked() or not self.navigation.is_valid:
            data["navigation"]["type"] = "free"
        else:
            data["navigation"]["type"] = self.navigation_dialog.QCBox_BuildNavType.currentText()
            data["navigation"]["tile_keep_visible"] = ThRasE.dialog.currentTileKeepVisible.isChecked()
            data["navigation"]["tile_size"] = self.navigation_dialog.tileSize.value()
            data["navigation"]["mode"] = \
                "horizontal" if self.navigation_dialog.nav_horizontal_mode.isChecked() else "vertical"
            data["navigation"]["tiles_color"] = self.navigation.tiles_color.name()
            data["navigation"]["current_tile_id"] = self.navigation.current_tile.idx
            data["navigation"]["size_dialog"] = (self.navigation_dialog.size().width(), self.navigation_dialog.size().height())
            data["navigation"]["extent_dialog"] = self.navigation_dialog.render_widget.canvas.extent().toRectF().getCoords()
            data["navigation"]["build_tools"] = self.navigation_dialog.QPBtn_BuildNavigationTools.isChecked()
            # special type navigation
            if data["navigation"]["type"] == "AOIs":
                aois = [[[[pl.x(), pl.y()] for pl in pls] for pls in aoi.asGeometry().asMultiPolygon()[0]][0]
                        for aoi in self.navigation_dialog.aoi_drawn]
                data["navigation"]["aois"] = aois
            if data["navigation"]["type"] in ["polygons",
                                              "points",
                                              "centroid of polygons"]:
                data["navigation"]["vector_file"] = \
                    setup_path(get_file_path_of_layer(self.navigation_dialog.QCBox_VectorFile.currentLayer()))

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
        import json, gzip, base64
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

        with open(file_out, 'w', encoding='utf-8') as yaml_file:
            yaml.dump(data, yaml_file, Dumper=dumper, default_flow_style=False, sort_keys=False)


class Pixel:
    def __eq__(self, other):
        return self.__hash__() == other.__hash__()

    def __hash__(self):
        return self._hash or hash((round(self.qgs_point.x(), LayerToEdit.current.pixel_tolerance),
                                   round(self.qgs_point.y(), LayerToEdit.current.pixel_tolerance)))

    def __init__(self, x=None, y=None, point=None):
        self._hash = None
        self.qgs_point = point if point is not None else QgsPointXY(x, y)

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
    """Class for store the edit events (pixels, lines, polygons, freehand) with the
    purpose to go undo or redo the edit actions by user

    For pixels:
        [(Pixel, value), ...]

    for polygons:
        [(polygon_feature, ((Pixel, value), ...)), ...]

    """

    def __init__(self, edit_type):
        self.edit_type = edit_type
        self.undos = []
        self.redos = []

    def can_be_undone(self):
        return len(self.undos) > 0

    def can_be_redone(self):
        return len(self.redos) > 0

    def get_current_status(self, edit_log_entry):
        if self.edit_type == "pixel":
            pixel, _ = edit_log_entry
            return pixel, LayerToEdit.current.get_pixel_value_from_pnt(pixel.qgs_point)
        if self.edit_type in ["line", "polygon"]:
            feature, pixel_values = edit_log_entry
            return feature, [(pixel, LayerToEdit.current.get_pixel_value_from_pnt(pixel.qgs_point)) for pixel, _ in pixel_values]

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
        self.undos.append(edit_log_entry)
        self.redos = []
