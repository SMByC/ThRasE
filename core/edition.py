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
import functools
import numpy as np
from dask import compute, delayed

from qgis.core import QgsRaster, QgsPointXY, QgsRasterBlock, Qgis, QgsGeometry

from ThRasE.core.navigation import Navigation
from ThRasE.utils.others_utils import get_xml_style
from ThRasE.utils.qgis_utils import get_file_path_of_layer
from ThRasE.utils.system_utils import wait_process


def edit_layer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # set layer for edit
        if not LayerToEdit.current.data_provider.isEditable():
            success = LayerToEdit.current.data_provider.setEditable(True)
            if not success:
                from ThRasE.thrase import ThRasE
                ThRasE.dialog.MsgBar.pushMessage("ThRasE has problems for modify this thematic raster", level=Qgis.Critical)
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
        self.bounds = layer.extent().toRectF().getCoords()
        self.navigation = Navigation()
        # store pixels: value, color, new_value, on/off
        #  -> [{"value": int, "color": {"R", "G", "B", "A"}, "new_value": int, "on": bool}, ...]
        self.pixels = None
        # init data for recode pixel table
        self.setup_pixel_table()
        # save editions of the layer using the picker edition tools
        self.history_pixels = History("pixels")
        self.history_lines = History("lines")
        self.history_polygons = History("polygons")

        LayerToEdit.instances[(layer.id(), band)] = self

    def extent(self):
        return self.qgs_layer.extent()

    def get_pixel_value_from_xy(self, x, y):
        return self.qgs_layer.dataProvider().identify(QgsPointXY(x, y), QgsRaster.IdentifyFormatValue).results()[self.band]

    def get_pixel_value_from_pnt(self, point):
        return self.qgs_layer.dataProvider().identify(point, QgsRaster.IdentifyFormatValue).results()[self.band]

    def setup_pixel_table(self):
        if self.pixels is None:
            xml_style_items = get_xml_style(self.qgs_layer, self.band)
            if xml_style_items is None:
                self.pixels = None
                return

            self.pixels = []
            for xml_item in xml_style_items:
                pixel = {"value": int(xml_item.get("value")), "color": {}, "new_value": None, "on": True}

                item_color = xml_item.get("color").lstrip('#')
                item_color = tuple(int(item_color[i:i + 2], 16) for i in (0, 2, 4))
                pixel["color"]["R"] = item_color[0]
                pixel["color"]["G"] = item_color[1]
                pixel["color"]["B"] = item_color[2]
                pixel["color"]["A"] = int(xml_item.get("alpha"))

                self.pixels.append(pixel)

    def get_the_new_pixel_value(self, point):
        old_value = self.get_pixel_value_from_pnt(point)
        # get the new value set in the recode pixel table by user
        new_value = next((i["new_value"] for i in self.pixels if i["value"] == old_value and i["on"]), None)
        if old_value != new_value:
            return new_value

    def select_value_in_recode_pixel_table(self, value_to_select):
        """Highlight the current pixel value from mouse picker"""
        from ThRasE.thrase import ThRasE
        if value_to_select is None:
            ThRasE.dialog.recodePixelTable.clearSelection()
            return

        row_idx = next((idx for idx, i in enumerate(self.pixels) if i["value"] == value_to_select), None)
        if row_idx is not None:
            ThRasE.dialog.recodePixelTable.setCurrentCell(row_idx, 1)
        else:
            ThRasE.dialog.recodePixelTable.clearSelection()

    def check_point_inside_layer(self, point):
        # check if the point is within active raster bounds
        if self.bounds[0] <= point.x() <= self.bounds[2] and self.bounds[1] <= point.y() <= self.bounds[3]:
            return True
        else:
            return False

    def edit_pixel(self, point, new_value=None):
        if not self.check_point_inside_layer(point) and not self.pixels:
            return

        if new_value is None:
            new_value = self.get_the_new_pixel_value(point)
            # check if the new value is valid and different
            if new_value is None:
                return

        px = int((point.x() - self.bounds[0]) / self.qgs_layer.rasterUnitsPerPixelX())  # num column position in x
        py = int((self.bounds[3] - point.y()) / self.qgs_layer.rasterUnitsPerPixelY())  # num row position in y

        rblock = QgsRasterBlock(self.data_provider.dataType(self.band), 1, 1)
        rblock.setValue(0, 0, new_value)
        write_status = self.data_provider.writeBlock(rblock, self.band, px, py)
        return write_status

    @wait_process
    @edit_layer
    def edit_from_pixel_picker(self, point):
        # get the pixel and value before edit it for save in history pixels class
        history_item = (point, self.get_pixel_value_from_pnt(point))
        # edit
        edit_status = self.edit_pixel(point)
        if edit_status:  # the pixel was edited
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            # save history item
            self.history_pixels.add(history_item)
            return True

    @wait_process
    @edit_layer
    def edit_from_line_picker(self, line_feature, line_buffer):
        if line_feature is None:
            return

        ps_x = self.qgs_layer.rasterUnitsPerPixelX()  # pixel size in x
        ps_y = self.qgs_layer.rasterUnitsPerPixelY()  # pixel size in y
        ps_avg = (ps_x + ps_y) / 2  # average of the pixel size, when the pixel is not square

        # function for check if the pixel must be edited (parallel process)
        def check_pixel_to_edit_in(x, y):
            pc_x = self.bounds[0] + int((x - self.bounds[0]) / ps_x)*ps_x + ps_x/2  # locate the pixel centroid in x
            pc_y = self.bounds[3] - int((self.bounds[3] - y) / ps_y)*ps_y - ps_y/2  # locate the pixel centroid in y

            point = QgsPointXY(pc_x, pc_y)
            if line_feature.geometry().distance(QgsGeometry.fromPointXY(point)) <= ps_avg*line_buffer:
                # return the pixel-point to edit
                return point

        # build dask process for edit pixels, edit line by segments of box of pair pixel consecutive
        points = line_feature.geometry().asPolyline()
        pixels_to_check = []
        for p1, p2 in zip(points[:-1], points[1:]):
            pixels_to_check += [
                delayed(check_pixel_to_edit_in)(x, y)
                for y in np.arange(min(p1.y(), p2.y())-ps_y*line_buffer, max(p1.y(), p2.y())+ps_y*line_buffer, ps_y)
                for x in np.arange(min(p1.x(), p2.x())-ps_x*line_buffer, max(p1.x(), p2.x())+ps_x*line_buffer, ps_x)]

        # compute with dask
        # the return all centroid points of pixels for edit
        pixels_to_process = compute(*pixels_to_check, scheduler='threads')
        pixels_to_process = set([item for item in pixels_to_process if item])  # clean None and duplicates

        # function for edit each pixel in parallel process
        def edit_pixel(pixel_point):
            # get the pixel and value before edit it for save in history pixels class
            point_and_value = (pixel_point, self.get_pixel_value_from_pnt(pixel_point))
            # edit
            edit_status = self.edit_pixel(pixel_point)
            if edit_status:  # the pixel was edited
                return point_and_value

        # compute with dask
        # the return all the pixel and value before edit it, for save in history class
        points_and_values = compute(*[delayed(edit_pixel)(pixel_point) for pixel_point in pixels_to_process],
                                    scheduler='threads')
        points_and_values = [item for item in points_and_values if item]  # clean None, unedited pixels

        if points_and_values:
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            # save history item
            self.history_lines.add((line_feature, points_and_values))
            return True

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
        # analysis all pixel if is inside the polygon drawn
        pixels_to_process = \
            [QgsPointXY(x, y)
             for y in np.arange(y_min, y_max + ps_y, ps_y)
             for x in np.arange(x_min, x_max + ps_x, ps_x)
             if polygon_feature.geometry().contains(QgsGeometry.fromPointXY(QgsPointXY(x, y)))]
        # clean None and duplicates
        pixels_to_process = set([item for item in pixels_to_process if item])

        # function for edit each pixel in parallel process
        def edit_pixel(pixel_point):
            # get the pixel and value before edit it for save in history pixels class
            point_and_value = (pixel_point, self.get_pixel_value_from_pnt(pixel_point))
            # edit
            edit_status = self.edit_pixel(pixel_point)
            if edit_status:  # the pixel was edited
                return point_and_value

        # edit and return all the pixel and value before edit it, for save in history class
        points_and_values = [edit_pixel(pixel_point) for pixel_point in pixels_to_process]
        points_and_values = [item for item in points_and_values if item]  # clean None, unedited pixels

        if points_and_values:
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            # save history item
            self.history_polygons.add((polygon_feature, points_and_values))
            return True


class History:
    """Class for store the items (pixels, lines or polygons) with the
    purpose to go undo or redo the edit actions by user

    For pixels:
        [(point, value), ...]

    for polygons:
        [(polygon_feature, ((point, value), ...)), ...]

    """
    max_history_items = 20

    def __init__(self, edit_type):
        self.edit_type = edit_type
        self.undos = []
        self.redos = []

    def can_be_undone(self):
        return len(self.undos) > 0

    def can_be_redone(self):
        return len(self.redos) > 0

    def get_current_status(self, item):
        if self.edit_type == "pixels":
            point = item[0]
            curr_value = LayerToEdit.current.get_pixel_value_from_pnt(point)
            return point, curr_value
        if self.edit_type in ["lines", "polygons"]:
            feature = item[0]
            points_and_values = item[1]
            curr_points_and_values = []
            for point, value in points_and_values:
                curr_points_and_values.append((point, LayerToEdit.current.get_pixel_value_from_pnt(point)))
            return feature, curr_points_and_values

    def undo(self):
        if self.can_be_undone():
            item = self.undos.pop()
            self.redos.append(self.get_current_status(item))
            return item

    def redo(self):
        if self.can_be_redone():
            item = self.redos.pop()
            self.undos.append(self.get_current_status(item))
            return item

    def add(self, item):
        self.undos.append(item)
        if len(self.undos) > History.max_history_items:
            del self.undos[0]
        self.redos = []

