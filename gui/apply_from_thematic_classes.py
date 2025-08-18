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
import uuid
from copy import deepcopy
from pathlib import Path
import numpy as np
from osgeo import gdal

from qgis.core import QgsMapLayerProxyModel, Qgis, QgsPointXY
from qgis.gui import QgsMapToolPan
from qgis.PyQt import uic
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QTableWidgetItem, QDialogButtonBox
from qgis.PyQt.QtCore import pyqtSlot, Qt

from ThRasE.core.editing import Pixel, LayerToEdit, edit_layer
from ThRasE.utils.others_utils import get_xml_style
from ThRasE.utils.qgis_utils import load_and_select_filepath_in, apply_symbology, get_file_path_of_layer
from ThRasE.utils.system_utils import block_signals_to, error_handler, wait_process

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'apply_from_thematic_classes.ui'))


class ApplyFromThematicClasses(QDialog, FORM_CLASS):

    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.setupUi(self)
        self.thematic_file_classes = None

        self.map_tool_pan = QgsMapToolPan(self.render_widget.canvas)
        self.render_widget.canvas.setMapTool(self.map_tool_pan, clean=True)

    def reject(self):
        self.restore_symbology()
        super().reject()

    def setup_gui(self):
        # clear
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)
        self.QCBox_ThematicFile.setCurrentIndex(-1)
        self.render_widget.canvas.setLayers([])
        self.render_widget.refresh()
        self.QCBox_band_ThematicFile.clear()
        # set properties to QgsMapLayerComboBox
        self.QCBox_ThematicFile.setFilters(QgsMapLayerProxyModel.RasterLayer)
        # ignore and not show the current thematic
        self.QCBox_ThematicFile.setExceptedLayerList([LayerToEdit.current.qgs_layer])
        # handle connect layer selection with render canvas
        self.QCBox_ThematicFile.layerChanged.connect(self.select_thematic_file_classes)
        self.QCBox_band_ThematicFile.currentIndexChanged.connect(self.setup_thematic_file_classes)
        # call to browse the render file
        self.QPBtn_BrowseThematicFile.clicked.connect(lambda: self.browser_dialog_to_load_file(
            self.QCBox_ThematicFile,
            dialog_title=self.tr("Select the thematic raster file"),
            file_filters=self.tr("Raster files (*.tif *.img);;All files (*.*)")))
        # for select the classes
        self.PixelTable.itemClicked.connect(self.table_item_clicked)
        # apply
        self.DialogButtons.button(QDialogButtonBox.Apply).clicked.connect(lambda: self.apply())

    @pyqtSlot()
    def browser_dialog_to_load_file(self, combo_box, dialog_title, file_filters):
        file_path, _ = QFileDialog.getOpenFileName(self, dialog_title, "", file_filters)
        if file_path != '' and os.path.isfile(file_path):
            # load to qgis and update combobox list
            load_and_select_filepath_in(combo_box, file_path, add_to_legend=False)

            self.select_thematic_file_classes(combo_box.currentLayer())

    def restore_symbology(self):
        """Restore the symbology of the thematic file classes to the original"""
        if self.thematic_file_classes and self.QCBox_band_ThematicFile.currentText():
            band = int(self.QCBox_band_ThematicFile.currentText())
            symbology = \
                [(str(pixel["value"]), pixel["value"],
                  (pixel["color"]["R"], pixel["color"]["G"], pixel["color"]["B"], pixel["color"]["A"]))
                 for pixel in self.pixel_classes_backup]
            apply_symbology(self.thematic_file_classes, band, symbology)

    def select_thematic_file_classes(self, layer):
        def clear():
            with block_signals_to(self.QCBox_ThematicFile):
                self.QCBox_ThematicFile.setCurrentIndex(-1)
            self.render_widget.canvas.setLayers([])
            self.render_widget.refresh()
            self.PixelTable.clear()
            self.PixelTable.setRowCount(0)
            self.PixelTable.setColumnCount(0)
            with block_signals_to(self.QCBox_band_ThematicFile):
                self.QCBox_band_ThematicFile.clear()

        if not layer:
            clear()
            return

        if layer.crs() != LayerToEdit.current.qgs_layer.crs():
            self.MsgBar.pushMessage("The selected file \"{}\" doesn't have the same coordinate system with respect to "
                                    "the thematic layer to edit \"{}\"".format(layer.name(), LayerToEdit.current.qgs_layer.name()),
                                    level=Qgis.Critical, duration=10)
            clear()
            return

        if (round(layer.rasterUnitsPerPixelX(), 3) != round(LayerToEdit.current.qgs_layer.rasterUnitsPerPixelX(), 3) or
            round(layer.rasterUnitsPerPixelY(), 3) != round(LayerToEdit.current.qgs_layer.rasterUnitsPerPixelY(), 3)):
            self.MsgBar.pushMessage("The selected file \"{}\" doesn't have the same pixel size with respect to "
                                    "the thematic layer to edit \"{}\"".format(layer.name(), LayerToEdit.current.qgs_layer.name()),
                                    level=Qgis.Critical, duration=10)
            clear()
            return

        self.render_widget.canvas.setDestinationCrs(layer.crs())
        self.render_widget.canvas.setLayers([layer])
        self.render_widget.canvas.setExtent(layer.extent())
        self.render_widget.refresh()
        self.thematic_file_classes = layer

        # set band count
        with block_signals_to(self.QCBox_band_ThematicFile):
            self.QCBox_band_ThematicFile.clear()
            self.QCBox_band_ThematicFile.addItems([str(x) for x in range(1, self.thematic_file_classes.bandCount() + 1)])

        self.setup_thematic_file_classes()

    @pyqtSlot()
    @error_handler
    def setup_thematic_file_classes(self):
        # extract pixel classes from file
        if not self.QCBox_band_ThematicFile.currentText():
            return
        band = int(self.QCBox_band_ThematicFile.currentText())
        xml_style_items = get_xml_style(self.thematic_file_classes, band)
        if xml_style_items is None:
            self.PixelTable.clear()
            self.PixelTable.setRowCount(0)
            self.PixelTable.setColumnCount(0)
            return
        self.pixel_classes = []
        for xml_item in xml_style_items:
            pixel = {"value": int(xml_item.get("value")), "color": {}, "select": False}
            item_color = xml_item.get("color").lstrip('#')
            item_color = tuple(int(item_color[i:i + 2], 16) for i in (0, 2, 4))
            pixel["color"]["R"] = item_color[0]
            pixel["color"]["G"] = item_color[1]
            pixel["color"]["B"] = item_color[2]
            pixel["color"]["A"] = int(xml_item.get("alpha"))
            self.pixel_classes.append(pixel)
        self.pixel_classes_backup = deepcopy(self.pixel_classes)

        self.set_pixel_table()

    def set_pixel_table(self):
        # clear table
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)
        if not self.thematic_file_classes:
            return

        with block_signals_to(self.PixelTable):
            header = ["", "class value", "select"]
            row_length = len(self.pixel_classes)
            # init table
            self.PixelTable.setRowCount(row_length)
            self.PixelTable.setColumnCount(3)
            self.PixelTable.horizontalHeader().setMinimumSectionSize(45)
            # hidden row labels
            self.PixelTable.verticalHeader().setVisible(False)
            # add Header
            self.PixelTable.setHorizontalHeaderLabels(header)
            # insert items
            for col_idx, header in enumerate(header):
                if header == "":
                    for row_idx, pixel in enumerate(self.pixel_classes):
                        item_table = QTableWidgetItem()
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setBackground(QColor(pixel["color"]["R"], pixel["color"]["G"],
                                                        pixel["color"]["B"], pixel["color"]["A"]))
                        self.PixelTable.setItem(row_idx, col_idx, item_table)
                if header == "class value":
                    for row_idx, pixel in enumerate(self.pixel_classes):
                        item_table = QTableWidgetItem(str(pixel["value"]))
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsEditable)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        self.PixelTable.setItem(row_idx, col_idx, item_table)
                if header == "select":
                    for row_idx, pixel in enumerate(self.pixel_classes):
                        item_table = QTableWidgetItem()
                        item_table.setFlags(item_table.flags() | Qt.ItemIsUserCheckable)
                        item_table.setFlags(item_table.flags() | Qt.ItemIsEnabled)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        if pixel["select"]:
                            item_table.setCheckState(Qt.Checked)
                        else:
                            item_table.setCheckState(Qt.Unchecked)
                        self.PixelTable.setItem(row_idx, col_idx, item_table)

            # adjust size of Table
            self.PixelTable.resizeColumnsToContents()
            self.PixelTable.resizeRowsToContents()
            # adjust the editor block based on table content
            table_width = self.PixelTable.horizontalHeader().length() + 40
            self.TableBlock.setMaximumWidth(table_width)
            self.TableBlock.setMinimumWidth(table_width)

    @pyqtSlot(QTableWidgetItem)
    def table_item_clicked(self, table_item):
        if table_item.text() == "none":
            return
        # set color
        if table_item.column() == 2:
            band = int(self.QCBox_band_ThematicFile.currentText())
            class_value = int(self.PixelTable.item(table_item.row(), 1).text())

            if table_item.checkState() == 0:
                self.pixel_classes[table_item.row()]["color"] = self.pixel_classes_backup[table_item.row()]["color"]
            if table_item.checkState() == 2:
                self.pixel_classes[table_item.row()]["color"] = {"R": 255, "G": 255, "B": 0, "A": 255}

            symbology = \
                [(str(pixel["value"]), pixel["value"],
                  (pixel["color"]["R"], pixel["color"]["G"], pixel["color"]["B"], pixel["color"]["A"]))
                 for pixel in self.pixel_classes]
            apply_symbology(self.thematic_file_classes, band, symbology)

    @wait_process
    @edit_layer
    def apply(self):
        pixel_table = self.PixelTable
        classes_selected = [int(pixel_table.item(row_idx, 1).text()) for row_idx in range(len(self.pixel_classes))
                            if pixel_table.item(row_idx, 2).checkState() == 2]

        if not classes_selected:
            self.MsgBar.pushMessage("Error: no class was selected to apply", level=Qgis.Warning, duration=5)
            return

        extent_intercepted = LayerToEdit.current.qgs_layer.extent().intersect(self.thematic_file_classes.extent())

        ps_x = self.thematic_file_classes.rasterUnitsPerPixelX()  # pixel size in x
        ps_y = self.thematic_file_classes.rasterUnitsPerPixelY()  # pixel size in y

        # locate the pixel centroid in x and y for the pixel in min/max in the extent
        y_min = extent_intercepted.yMinimum() + (ps_y / 2.0)
        y_max = extent_intercepted.yMaximum() - (ps_y / 2.0)
        x_min = extent_intercepted.xMinimum() + (ps_x / 2.0)
        x_max = extent_intercepted.xMaximum() - (ps_x / 2.0)
        # index for extent intercepted start in the corner left-top respect to thematic file classes
        idx_x = int((x_min - self.thematic_file_classes.extent().xMinimum()) / ps_x)
        idx_y = int((self.thematic_file_classes.extent().yMaximum() - y_max) / ps_y)

        thematic_classes_band = int(self.QCBox_band_ThematicFile.currentText())

        # data array for the extent intercepted using the thematic file classes values
        ds_in = gdal.Open(get_file_path_of_layer(self.thematic_file_classes))
        da_intercepted = ds_in.GetRasterBand(thematic_classes_band).ReadAsArray(
            idx_x, idx_y, int(round((x_max-x_min)/ps_x + 1)), int(round((y_max-y_min)/ps_y + 1))).astype(int)
        del ds_in

        pixels_to_process = \
            [Pixel(x=x, y=y)
             for n_y, y in enumerate(np.arange(y_min, y_max + ps_y/2.0, ps_y)[::-1])
             for n_x, x in enumerate(np.arange(x_min, x_max + ps_x/2.0, ps_x))
             if da_intercepted[n_y][n_x] in classes_selected]
        del da_intercepted

        # edit all pixels inside the classes selected based on the recode pixel table
        group_id = uuid.uuid4()
        edit_status = [LayerToEdit.current.edit_pixel(pixel, group_id=group_id) for pixel in pixels_to_process]
        if edit_status:
            if hasattr(LayerToEdit.current.qgs_layer, 'setCacheImage'):
                LayerToEdit.current.qgs_layer.setCacheImage(None)
            LayerToEdit.current.qgs_layer.reload()
            LayerToEdit.current.qgs_layer.triggerRepaint()
        else:
            self.MsgBar.pushMessage("No pixels were edited because the selected classes do not overlap the areas of the"
                                    "classes to modify", level=Qgis.Info, duration=5)
            return

        # finish the edition
        self.restore_symbology()
        # clear
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)
        self.render_widget.canvas.setLayers([])
        self.render_widget.canvas.clearCache()
        self.render_widget.refresh()
        self.QCBox_ThematicFile.setCurrentIndex(-1)
        with block_signals_to(self.QCBox_band_ThematicFile):
            self.QCBox_band_ThematicFile.clear()

        self.accept()
