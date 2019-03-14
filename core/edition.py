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
import numpy as np

from qgis.core import QgsRaster, QgsPointXY, QgsRasterBlock, Qgis, QgsGeometry

from ThRasE.core.navigation import Navigation
from ThRasE.utils.others_utils import get_xml_style
from ThRasE.utils.qgis_utils import get_file_path_of_layer
from ThRasE.utils.system_utils import wait_process


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
        #  -> [{"value", "color": {"R", "G", "B", "A"}, "new_value": int, "on": bool}, ...]
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
        new_value = [i["new_value"] for i in self.pixels if i["value"] == old_value and i["on"]]
        if new_value and new_value[0] is not None and old_value != new_value[0]:
            return new_value[0]

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

        if not self.data_provider.isEditable():
            success = self.data_provider.setEditable(True)
            if not success:
                from ThRasE.thrase import ThRasE
                ThRasE.dialog.MsgBar.pushMessage("ThRasE has problems for modify this thematic raster", level=Qgis.Warning)
                return

        rblock = QgsRasterBlock(self.data_provider.dataType(self.band), 1, 1)
        rblock.setValue(0, 0, new_value)
        success = self.data_provider.writeBlock(rblock, self.band, px, py)
        if not success:
            from ThRasE.thrase import ThRasE
            ThRasE.dialog.MsgBar.pushMessage("ThRasE has problems for modify this thematic raster", level=Qgis.Warning)
            return
        self.data_provider.setEditable(False)

        return True

    @wait_process
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
    def edit_from_line_picker(self, line_feature, line_buffer):
        if line_feature is None:
            return

        box = line_feature.geometry().boundingBox()
        ps_x = self.qgs_layer.rasterUnitsPerPixelX()  # pixel size in x
        ps_y = self.qgs_layer.rasterUnitsPerPixelY()  # pixel size in y
        ps_avg = (ps_x + ps_y) / 2  # average of the pixel size, when the pixel is not square

        # all the pixel and value before edit it for save in history polygons class
        points_and_values = []  # (point, self.get_pixel_value_from_pnt(point))

        for x in np.arange(box.xMinimum()-ps_x*line_buffer, box.xMaximum()+ps_x*line_buffer, ps_x):
            for y in np.arange(box.yMinimum()-ps_y*line_buffer, box.yMaximum()+ps_y*line_buffer, ps_y):
                pc_x = self.bounds[0] + int((x - self.bounds[0]) / ps_x)*ps_x + ps_x/2  # locate the pixel centroid in x
                pc_y = self.bounds[3] - int((self.bounds[3] - y) / ps_y)*ps_y - ps_y/2  # locate the pixel centroid in y

                point = QgsPointXY(pc_x, pc_y)
                if line_feature.geometry().distance(QgsGeometry.fromPointXY(point)) <= ps_avg*line_buffer:
                    # get the pixel and value before edit it for save in history pixels class
                    point_and_value = (point, self.get_pixel_value_from_pnt(point))
                    # edit
                    edit_status = self.edit_pixel(point)
                    if edit_status:  # the pixel was edited
                        points_and_values.append(point_and_value)

        if points_and_values:
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            # save history item
            self.history_lines.add((line_feature, points_and_values))
            return True

    @wait_process
    def edit_from_polygon_picker(self, polygon_feature):
        if polygon_feature is None:
            return

        box = polygon_feature.geometry().boundingBox()
        ps_x = self.qgs_layer.rasterUnitsPerPixelX()  # pixel size in x
        ps_y = self.qgs_layer.rasterUnitsPerPixelY()  # pixel size in y

        # all the pixel and value before edit it for save in history polygons class
        points_and_values = []  # (point, self.get_pixel_value_from_pnt(point))

        for x in np.arange(box.xMinimum()+ps_x/2, box.xMaximum(), ps_x):
            for y in np.arange(box.yMinimum()+ps_y/2, box.yMaximum(), ps_y):
                pc_x = self.bounds[0] + int((x - self.bounds[0]) / ps_x)*ps_x + ps_x/2  # locate the pixel centroid in x
                pc_y = self.bounds[3] - int((self.bounds[3] - y) / ps_y)*ps_y - ps_y/2  # locate the pixel centroid in y

                point = QgsPointXY(pc_x, pc_y)
                if polygon_feature.geometry().contains(QgsGeometry.fromPointXY(point)):
                    # get the pixel and value before edit it for save in history pixels class
                    point_and_value = (point, self.get_pixel_value_from_pnt(point))
                    # edit
                    edit_status = self.edit_pixel(point)
                    if edit_status:  # the pixel was edited
                        points_and_values.append(point_and_value)

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

