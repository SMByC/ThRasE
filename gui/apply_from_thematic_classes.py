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
from shutil import move
import numpy as np
from osgeo import gdal

from qgis.core import QgsMapLayerProxyModel, Qgis
from qgis.gui import QgsMapToolPan
from qgis.PyQt import uic
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QTableWidgetItem, QDialogButtonBox
from qgis.PyQt.QtCore import pyqtSlot, Qt

from ThRasE.core.editing import Pixel, PixelLog, LayerToEdit
from ThRasE.utils.others_utils import get_xml_style, copy_band_metadata, copy_dataset_metadata
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
        try: self.DialogButtons.button(QDialogButtonBox.Apply).clicked.disconnect()
        except TypeError: pass
        self.DialogButtons.button(QDialogButtonBox.Apply).clicked.connect(lambda: self.apply())
        # registry
        registry_enabled = LayerToEdit.current.registry.enabled if LayerToEdit.current else False
        self.RecordChangesInRegistry.setChecked(False)
        self.RecordChangesInRegistry.setEnabled(registry_enabled)
        # Set tooltip based on registry status
        tooltip_base = (
            "<p>Add the changes that will be applied here to the ThRasE registry.</p>" +
            "<p>Note: Be aware of the image size and number of pixels that will change.</p>"
        )
        if registry_enabled:
            tooltip = f"<html><head/><body>{tooltip_base}</body></html>"
        else:
            tooltip_notice = "<p><b>Registry is disabled:</b> enable it in the main dialog to store these edits.</p>"
            tooltip = f"<html><head/><body>{tooltip_base}{tooltip_notice}</body></html>"
        self.RecordChangesInRegistry.setToolTip(tooltip)

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
                                    level=Qgis.Critical, duration=20)
            clear()
            return

        if (round(layer.rasterUnitsPerPixelX(), 3) != round(LayerToEdit.current.qgs_layer.rasterUnitsPerPixelX(), 3) or
            round(layer.rasterUnitsPerPixelY(), 3) != round(LayerToEdit.current.qgs_layer.rasterUnitsPerPixelY(), 3)):
            self.MsgBar.pushMessage("The selected file \"{}\" doesn't have the same pixel size with respect to "
                                    "the thematic layer to edit \"{}\"".format(layer.name(), LayerToEdit.current.qgs_layer.name()),
                                    level=Qgis.Critical, duration=20)
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
            try:
                # Validate required attributes are present
                value = xml_item.get("value")
                color = xml_item.get("color")
                alpha = xml_item.get("alpha")

                if value is None or color is None or alpha is None:
                    continue  # Skip items with missing attributes

                pixel = {"value": int(value), "color": {}, "select": False}
                item_color = color.lstrip('#')
                item_color = tuple(int(item_color[i:i + 2], 16) for i in (0, 2, 4))
                pixel["color"]["R"] = item_color[0]
                pixel["color"]["G"] = item_color[1]
                pixel["color"]["B"] = item_color[2]
                pixel["color"]["A"] = int(alpha)
                self.pixel_classes.append(pixel)
            except (ValueError, AttributeError, TypeError):
                # Skip items with invalid data
                continue

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

    @pyqtSlot(QTableWidgetItem)
    def table_item_clicked(self, table_item):
        if table_item.text() == "none":
            return
        # set color when checkbox in "select" column is clicked
        if table_item.column() == 2:
            band = int(self.QCBox_band_ThematicFile.currentText())
            row_idx = table_item.row()

            if table_item.checkState() == Qt.Unchecked:
                self.pixel_classes[row_idx]["color"] = self.pixel_classes_backup[row_idx]["color"]
            elif table_item.checkState() == Qt.Checked:
                self.pixel_classes[row_idx]["color"] = {"R": 255, "G": 255, "B": 0, "A": 255}

            symbology = \
                [(str(pixel["value"]), pixel["value"],
                  (pixel["color"]["R"], pixel["color"]["G"], pixel["color"]["B"], pixel["color"]["A"]))
                 for pixel in self.pixel_classes]
            apply_symbology(self.thematic_file_classes, band, symbology)

    @wait_process
    def apply(self):
        """Apply changes within selected classes from another thematic raster"""

        from ThRasE.thrase import ThRasE

        pixel_table = self.PixelTable
        if pixel_table.rowCount() == 0:
            self.MsgBar.pushMessage("Error: pixel table for class selection is empty", level=Qgis.Warning, duration=10)
            return
        classes_selected = [int(pixel_table.item(row_idx, 1).text()) for row_idx in range(len(self.pixel_classes))
                            if pixel_table.item(row_idx, 2).checkState() == 2]

        if not classes_selected:
            self.MsgBar.pushMessage("Error: no class was selected to apply", level=Qgis.Warning, duration=10)
            return

        extent_intercepted = LayerToEdit.current.qgs_layer.extent().intersect(self.thematic_file_classes.extent())
        if extent_intercepted.isEmpty():
            self.MsgBar.pushMessage("No overlap was found between the thematic file and the layer to edit",
                                    level=Qgis.Info, duration=10)
            return

        record_changes = self.RecordChangesInRegistry.isChecked() and LayerToEdit.current.registry.enabled
        edited_pixels_count = 0
        row_indices = col_indices = None
        old_values = new_values = None

        try:
            # Read the layer to edit using GDAL
            layer_to_edit_path = LayerToEdit.current.file_path
            ds_in = gdal.Open(layer_to_edit_path, gdal.GA_ReadOnly)
            if ds_in is None:
                raise RuntimeError(f"Unable to open raster {layer_to_edit_path}")

            num_bands = ds_in.RasterCount
            src_band = ds_in.GetRasterBand(LayerToEdit.current.band)
            data_array = src_band.ReadAsArray().astype(int)
            new_data_array = deepcopy(data_array)

            # Read the thematic classes file
            thematic_classes_band = int(self.QCBox_band_ThematicFile.currentText())
            classes_ds = gdal.Open(get_file_path_of_layer(self.thematic_file_classes), gdal.GA_ReadOnly)
            if classes_ds is None:
                raise RuntimeError("Unable to open the thematic classes file")

            # Get georeferencing information
            layer_gt = ds_in.GetGeoTransform()
            classes_gt = classes_ds.GetGeoTransform()

            ps_x = layer_gt[1]  # pixel size in x
            ps_y = abs(layer_gt[5])  # pixel size in y (absolute value)

            # Calculate pixel indices in both rasters for the overlap extent
            x_min = extent_intercepted.xMinimum()
            x_max = extent_intercepted.xMaximum()
            y_min = extent_intercepted.yMinimum()
            y_max = extent_intercepted.yMaximum()

            # Indices in layer to edit
            layer_idx_x = int(round((x_min - layer_gt[0]) / ps_x))
            layer_idx_y = int(round((layer_gt[3] - y_max) / ps_y))

            # Indices in thematic classes
            classes_idx_x = int(round((x_min - classes_gt[0]) / ps_x))
            classes_idx_y = int(round((classes_gt[3] - y_max) / ps_y))

            # Calculate the number of pixels to process
            cols = int(round((x_max - x_min) / ps_x))
            rows = int(round((y_max - y_min) / ps_y))

            if cols <= 0 or rows <= 0:
                self.MsgBar.pushMessage("No pixels were identified within the overlap extent",
                                        level=Qgis.Info, duration=10)
                del ds_in, classes_ds
                return

            # Clamp to valid bounds for layer to edit
            layer_idx_x = max(0, layer_idx_x)
            layer_idx_y = max(0, layer_idx_y)
            cols = min(cols, ds_in.RasterXSize - layer_idx_x)
            rows = min(rows, ds_in.RasterYSize - layer_idx_y)

            # Clamp to valid bounds for classes
            classes_idx_x = max(0, classes_idx_x)
            classes_idx_y = max(0, classes_idx_y)
            cols = min(cols, classes_ds.RasterXSize - classes_idx_x)
            rows = min(rows, classes_ds.RasterYSize - classes_idx_y)

            if cols <= 0 or rows <= 0:
                self.MsgBar.pushMessage("No pixels to read within valid raster bounds",
                                        level=Qgis.Info, duration=10)
                del ds_in, classes_ds
                return

            # Read the class array from the overlap region
            class_array = classes_ds.GetRasterBand(thematic_classes_band).ReadAsArray(
                classes_idx_x, classes_idx_y, cols, rows).astype(int)
            del classes_ds

            # Create mask for selected classes
            mask_classes = np.isin(class_array, classes_selected)
            if not mask_classes.any():
                self.MsgBar.pushMessage("No pixels match the selected classes in the overlap area",
                                        level=Qgis.Info, duration=10)
                del ds_in
                return

            # Extract the corresponding region from the layer to edit
            overlap_data = data_array[layer_idx_y:layer_idx_y + rows, layer_idx_x:layer_idx_x + cols]

            # Apply the recode table only where classes match
            for old_value, new_value in LayerToEdit.current.old_new_value.items():
                # Apply only where the class matches AND the pixel value equals old_value
                change_mask = mask_classes & (overlap_data == old_value)
                new_data_array[layer_idx_y:layer_idx_y + rows, layer_idx_x:layer_idx_x + cols][change_mask] = new_value

            # Compute which pixels actually changed
            row_indices, col_indices = np.nonzero(new_data_array != data_array)
            edited_pixels_count = int(row_indices.size)

            if edited_pixels_count == 0:
                self.MsgBar.pushMessage("No pixels were edited (selected classes may not require changes in the target layer)",
                                        level=Qgis.Info, duration=10)
                del ds_in
                return

            if record_changes:
                old_values = data_array[row_indices, col_indices]
                new_values = new_data_array[row_indices, col_indices]

            # Create temporary output file
            fn, ext = os.path.splitext(layer_to_edit_path)
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
                if band_index == LayerToEdit.current.band:
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
            move(fn_out, layer_to_edit_path)

            # Record the changes in ThRasE registry
            if record_changes and edited_pixels_count:
                xmin, ymin, xmax, ymax = LayerToEdit.current.bounds
                group_id = uuid.uuid4()
                for row_idx, col_idx, old_val, new_val in zip(row_indices, col_indices, old_values, new_values):
                    x_coord = xmin + (float(col_idx) + 0.5) * ps_x
                    y_coord = ymax - (float(row_idx) + 0.5) * ps_y
                    PixelLog(Pixel(x=x_coord, y=y_coord), int(old_val), int(new_val), group_id, store=True)

            del new_data_array, data_array, class_array, mask_classes
            if old_values is not None:
                del old_values, new_values
            if row_indices is not None:
                del row_indices, col_indices

        except Exception as e:
            self.MsgBar.pushMessage(f"ERROR: {e}", level=Qgis.Critical, duration=20)
            return

        if hasattr(LayerToEdit.current.qgs_layer, 'setCacheImage'):
            LayerToEdit.current.qgs_layer.setCacheImage(None)
        LayerToEdit.current.qgs_layer.reload()
        LayerToEdit.current.qgs_layer.triggerRepaint()

        ThRasE.dialog.editing_status.setText(f"{edited_pixels_count} pixels edited!")
        if record_changes and edited_pixels_count:
            ThRasE.dialog.registry_widget.update_registry()

        self.MsgBar.pushMessage(
            "DONE: Changes were successfully applied within selected classes",
            level=Qgis.Success, duration=10)

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
