# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ThRasE
 
 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2024 by Xavier Corredor Llano, SMByC
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
import functools
import numpy as np
from copy import deepcopy
from shutil import move
from osgeo import gdal
from collections import OrderedDict
import yaml
try:
    from yaml import CDumper as Dumper
except ImportError:
    from yaml import Dumper

from qgis.core import QgsRaster, QgsPointXY, QgsRasterBlock, Qgis, QgsGeometry
from qgis.PyQt.QtCore import Qt

from ThRasE.core.navigation import Navigation
from ThRasE.gui.navigation_dialog import NavigationDialog
from ThRasE.utils.others_utils import get_xml_style
from ThRasE.utils.qgis_utils import get_file_path_of_layer, apply_symbology
from ThRasE.utils.system_utils import wait_process, block_signals_to


def check_before_editing():
    from ThRasE.thrase import ThRasE
    # check if the recode pixel table is empty
    if not LayerToEdit.current.old_new_value:
        ThRasE.dialog.MsgBar.pushMessage(
            "There are no changes to apply in the recode pixel table. Please set new pixel values first",
            level=Qgis.Warning, duration=5)
        return False
    return True


def edit_layer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # set layer for edit
        if not LayerToEdit.current.data_provider.isEditable():
            if not LayerToEdit.current.data_provider.setEditable(True):
                from ThRasE.thrase import ThRasE
                ThRasE.dialog.MsgBar.pushMessage("The current thematic raster cannot be edited due to layer restrictions or permission issues",
                                                 level=Qgis.Critical, duration=5)
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
        self.navigation_dialog = NavigationDialog(layer_to_edit=self)
        # store pixels: value, color, new_value, on/off
        #   -> [{"value": int, "color": {"R", "G", "B", "A"}, "new_value": int, "s/h": bool}, ...]
        self.pixels_backup = None  # backup for save the original values
        self.pixels = None
        # user personalization of value-color table of class pixels
        #   -> [("name", value, (R, G, B, A)), ...]
        self.symbology = None
        # dictionary for quick search the new value based on the old value in the recode table
        self.old_new_value = {}
        # save editions of the layer using the picker edition tools
        self.history_pixels = History("pixels")
        self.history_lines = History("lines")
        self.history_polygons = History("polygons")
        self.history_freehand = History("polygons")
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

    def get_the_old_new_pixel_value(self, point):
        old_value = self.get_pixel_value_from_pnt(point)
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

    def check_point_inside_layer(self, point):
        # check if the point is within active raster bounds
        return True if self.bounds[0] <= point.x() <= self.bounds[2] \
                       and self.bounds[1] <= point.y() <= self.bounds[3] else False

    def edit_pixel(self, point, new_value=None, check_bounds=True, inside_geom=None):
        if check_bounds and not self.check_point_inside_layer(point):
            return

        if new_value is None:
            old_value, new_value = self.get_the_old_new_pixel_value(point)
            if new_value is None:
                return
        else:
            old_value = self.get_pixel_value_from_pnt(point)

        if inside_geom and not inside_geom.contains(QgsGeometry.fromPointXY(point)):
            return

        px = int((point.x() - self.bounds[0]) / self.qgs_layer.rasterUnitsPerPixelX())  # num column position in x
        py = int((self.bounds[3] - point.y()) / self.qgs_layer.rasterUnitsPerPixelY())  # num row position in y

        rblock = QgsRasterBlock(self.data_provider.dataType(self.band), 1, 1)
        rblock.setValue(0, 0, new_value)
        if self.data_provider.writeBlock(rblock, self.band, px, py):  # write and check if writing status is ok
            return point, old_value

    @wait_process
    @edit_layer
    def edit_from_pixel_picker(self, point):
        history_item = self.edit_pixel(point)

        from ThRasE.thrase import ThRasE
        ThRasE.dialog.editing_status.setText("Pixel editing tool: {} pixel edited!".format(1 if history_item else 0))

        if history_item:  # the pixel was edited
            if hasattr(self.qgs_layer, 'setCacheImage'):
                self.qgs_layer.setCacheImage(None)
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

        # function for check if the pixel must be edited
        def check_pixel_to_edit_in(x, y):
            pc_x = self.bounds[0] + int((x - self.bounds[0]) / ps_x)*ps_x + ps_x/2  # locate the pixel centroid in x
            pc_y = self.bounds[3] - int((self.bounds[3] - y) / ps_y)*ps_y - ps_y/2  # locate the pixel centroid in y

            point = QgsPointXY(pc_x, pc_y)
            if line_feature.geometry().distance(QgsGeometry.fromPointXY(point)) <= ps_avg*line_buffer:
                # return the pixel-point to edit
                return point

        # analysis all pixel if is inside in the segments of box of pair pixel consecutive
        points = line_feature.geometry().asPolyline()
        pixels_to_process = \
            [[check_pixel_to_edit_in(x, y)
              for y in np.arange(min(p1.y(), p2.y())-ps_y*line_buffer, max(p1.y(), p2.y())+ps_y*line_buffer, ps_y)
              for x in np.arange(min(p1.x(), p2.x())-ps_x*line_buffer, max(p1.x(), p2.x())+ps_x*line_buffer, ps_x)]
             for p1, p2 in zip(points[:-1], points[1:])]
        # flat the list, clean None and duplicates
        pixels_to_process = set([item for sublist in pixels_to_process for item in sublist if item])

        # edit and return all the pixel and value before edit it, for save in history class
        history_edition_entry = [self.edit_pixel(pixel_point) for pixel_point in pixels_to_process]
        history_edition_entry = [item for item in history_edition_entry if item]  # clean None, unedited pixels

        from ThRasE.thrase import ThRasE
        ThRasE.dialog.editing_status.setText("Line editing tool: {} pixels edited!".format(len(history_edition_entry)))

        if history_edition_entry:
            if hasattr(self.qgs_layer, 'setCacheImage'):
                self.qgs_layer.setCacheImage(None)
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            # save history item
            self.history_lines.add((line_feature, history_edition_entry))
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

        # edit and return all the pixel and value before edit it, for save in history class
        polygon_geom = polygon_feature.geometry()
        history_edition_entry = \
            [self.edit_pixel(QgsPointXY(x, y), inside_geom=polygon_geom)
             for y in np.arange(y_min, y_max + ps_y, ps_y)
             for x in np.arange(x_min, x_max + ps_x, ps_x)]
        history_edition_entry = [item for item in history_edition_entry if item]  # clean None, unedited pixels

        from ThRasE.thrase import ThRasE
        ThRasE.dialog.editing_status.setText("Polygon editing tool: {} pixels edited!".format(len(history_edition_entry)))

        if history_edition_entry:
            if hasattr(self.qgs_layer, 'setCacheImage'):
                self.qgs_layer.setCacheImage(None)
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            # save history item
            self.history_polygons.add((polygon_feature, history_edition_entry))
            return True

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

        # edit and return all the pixel and value before edit it, for save in history class
        freehand_geom = freehand_feature.geometry()
        history_edition_entry = \
            [self.edit_pixel(QgsPointXY(x, y), inside_geom=freehand_geom)
             for y in np.arange(y_min, y_max + ps_y, ps_y)
             for x in np.arange(x_min, x_max + ps_x, ps_x)]
        history_edition_entry = [item for item in history_edition_entry if item]  # clean None, unedited pixels

        from ThRasE.thrase import ThRasE
        ThRasE.dialog.editing_status.setText("Freehand editing tool: {} pixels edited!".format(len(history_edition_entry)))

        if history_edition_entry:
            if hasattr(self.qgs_layer, 'setCacheImage'):
                self.qgs_layer.setCacheImage(None)
            self.qgs_layer.reload()
            self.qgs_layer.triggerRepaint()
            # save history item
            self.history_freehand.add((freehand_feature, history_edition_entry))
            return True

    @wait_process
    @edit_layer
    def edit_whole_image(self):
        # check if the image is relative small for process pixel by pixel
        if self.qgs_layer.width() * self.qgs_layer.height() <= 100000:
            ps_x = self.qgs_layer.rasterUnitsPerPixelX()  # pixel size in x
            ps_y = self.qgs_layer.rasterUnitsPerPixelY()  # pixel size in y

            # define x and y min/max in the extent
            y_min = self.bounds[1] + ps_y / 2
            y_max = self.bounds[3] - ps_y / 2
            x_min = self.bounds[0] + ps_x / 2
            x_max = self.bounds[2] - ps_x / 2

            # edit all the pixel using recode table
            [self.edit_pixel(QgsPointXY(x, y), check_bounds=False)
             for y in np.arange(y_min, y_max + ps_y, ps_y)
             for x in np.arange(x_min, x_max + ps_x, ps_x)]
        else:
            # read
            ds_in = gdal.Open(self.file_path)
            num_bands = ds_in.RasterCount
            data_array = ds_in.GetRasterBand(self.band).ReadAsArray().astype(int)
            new_data_array = deepcopy(data_array)

            # apply changes
            for old_value, new_value in self.old_new_value.items():
                new_data_array[data_array == old_value] = new_value
            del data_array

            # create file
            fn, ext = os.path.splitext(self.file_path)
            fn_out = fn + "_tmp" + ext
            driver = gdal.GetDriverByName(ds_in.GetDriver().ShortName)
            (x, y) = new_data_array.shape
            ds_out = driver.Create(fn_out, y, x, num_bands, ds_in.GetRasterBand(self.band).DataType)

            for b in range(1, num_bands + 1):
                if b == self.band:
                    ds_out.GetRasterBand(b).WriteArray(new_data_array)
                else:
                    ds_out.GetRasterBand(b).WriteArray(ds_in.GetRasterBand(b).ReadAsArray())
                nodata = ds_in.GetRasterBand(b).GetNoDataValue()
                if nodata is not None:
                    ds_out.GetRasterBand(b).SetNoDataValue(nodata)

            # set output geo info based on first input layer
            ds_out.SetGeoTransform(ds_in.GetGeoTransform())
            ds_out.SetProjection(ds_in.GetProjection())

            del ds_in, ds_out, driver, new_data_array
            move(fn_out, self.file_path)

        if hasattr(self.qgs_layer, 'setCacheImage'):
            self.qgs_layer.setCacheImage(None)
        self.qgs_layer.reload()
        self.qgs_layer.triggerRepaint()

    @wait_process
    def save_config(self, file_out):
        from ThRasE.thrase import ThRasE
        # save in class
        self.config_file = file_out

        def setup_yaml():
            """Keep dump ordered with orderedDict"""
            represent_dict_order = lambda self, data: self.represent_mapping('tag:yaml.org,2002:map', list(data.items()))
            yaml.add_representer(OrderedDict, represent_dict_order)

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

        setup_yaml()

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
        # view_widgets, active layers and edit tool
        data["active_layers_widget"] = ThRasE.dialog.QPBtn_ActiveLayers.isChecked()
        data["number_active_layers"] = ThRasE.dialog.QCBox_NoActiveLayers.currentText()
        data["edition_tools_widget"] = ThRasE.dialog.QPBtn_EditionTools.isChecked()
        # save the extent in the views using a view with a valid layer (not empty)
        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            if view_widget.is_active and not view_widget.render_widget.canvas.extent().isEmpty():
                data["extent"] = view_widget.render_widget.canvas.extent().toRectF().getCoords()
                break
        # view_widgets, active layers and edit tool
        data["view_widgets"] = []
        for view_widget in ThRasEDialog.view_widgets:
            active_layers = []
            for active_layer in view_widget.active_layers:
                active_layers.append({"is_active": active_layer.OnOffActiveLayer.isChecked(),
                                      "layer_name": active_layer.layer.name() if active_layer.layer else None,
                                      "layer_path": setup_path(get_file_path_of_layer(active_layer.layer)),
                                      "opacity": active_layer.opacity})
            data["view_widgets"].append({"active_layers": active_layers,
                                         "mouse_pixel_value": view_widget.mousePixelValue2Table.isChecked(),
                                         "pixels_picker_enabled": view_widget.PixelsPicker.isChecked(),
                                         "lines_picker_enabled": view_widget.LinesPicker.isChecked(),
                                         "line_buffer": view_widget.LineBuffer.currentText(),
                                         "lines_color": view_widget.lines_color.name(),
                                         "polygons_picker_enabled": view_widget.PolygonsPicker.isChecked(),
                                         "polygons_color": view_widget.polygons_color.name(),
                                         "freehand_picker_enabled": view_widget.FreehandPicker.isChecked(),
                                         "freehand_color": view_widget.freehand_color.name()})
        # navigation
        data["navigation"] = {}

        if not ThRasE.dialog.QPBtn_EnableNavigation.isChecked() or not self.navigation.is_valid:
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

        # CCD plugin config
        if ThRasE.dialog.ccd_plugin_available:
            from CCD_Plugin.utils.config import get_plugin_config
            data["ccd_plugin_config"] = get_plugin_config(ThRasE.dialog.ccd_plugin.id)
            data["ccd_plugin_opened"] = ThRasE.dialog.QPBtn_CCDPlugin.isChecked()

        with open(file_out, 'w') as yaml_file:
            yaml.dump(data, yaml_file, Dumper=Dumper)


class History:
    """Class for store the items (pixels, lines, polygons, freehand) with the
    purpose to go undo or redo the edit actions by user

    For pixels:
        [(point, value), ...]

    for polygons:
        [(polygon_feature, ((point, value), ...)), ...]

    """

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
            history_edition_entry = item[1]
            curr_history_edition_entry = []
            for point, value in history_edition_entry:
                curr_history_edition_entry.append((point, LayerToEdit.current.get_pixel_value_from_pnt(point)))
            return feature, curr_history_edition_entry

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
        self.redos = []

