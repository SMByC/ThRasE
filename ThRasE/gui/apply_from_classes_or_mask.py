# -*- coding: utf-8 -*-
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
import uuid
from copy import deepcopy
from pathlib import Path
from shutil import move
import numpy as np
from osgeo import gdal, ogr, osr

from qgis.core import (QgsMapLayerProxyModel, Qgis, QgsMapLayer,
                       QgsSingleSymbolRenderer, QgsFillSymbol, QgsWkbTypes)
from qgis.gui import QgsMapToolPan
from qgis.PyQt import uic
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QDialog, QTableWidgetItem, QDialogButtonBox
from qgis.PyQt.QtCore import pyqtSlot, Qt

from ThRasE.core.editing import Pixel, PixelLog, LayerToEdit
from ThRasE.utils.others_utils import get_xml_style, copy_band_metadata, copy_dataset_metadata
from ThRasE.utils.qgis_utils import browse_dialog_to_load_file, apply_symbology, get_source_from, \
    remove_layers_hidden_from_legend
from ThRasE.utils.system_utils import block_signals_to, error_handler, wait_process

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'apply_from_classes_or_mask.ui'))

# Yellow highlight used to mark the mask area on the canvas
VECTOR_MASK_FILL_RGBA = (255, 255, 0, 120)    # polygon fill on the canvas
VECTOR_MASK_OUTLINE_RGBA = (200, 160, 0, 255) # polygon outline on the canvas
VECTOR_MASK_TABLE_RGBA = (255, 255, 0, 255)  # table vector mask (fully opaque)


class ApplyFromClassesOrMask(QDialog, FORM_CLASS):

    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.setupUi(self)
        # raster mask state
        self.raster_mask_layer = None
        self.pixel_classes = []
        self.pixel_classes_backup = []
        # vector mask state
        self.vector_mask_layer = None
        self.vector_mask_renderer_backup = None

        self.map_tool_pan = QgsMapToolPan(self.render_widget.canvas)
        self.render_widget.canvas.setMapTool(self.map_tool_pan, clean=True)

    def reject(self):
        """Restore the mask layer symbology and drop any layer that was loaded
        hidden from the QGIS legend before closing the dialog."""
        self.restore_mask_symbology()
        remove_layers_hidden_from_legend()
        super().reject()

    def setup_gui(self):
        """Initialize the dialog: reset widgets, wire signals, accept both raster
        and polygon vector layers as mask sources."""
        # reset widgets to a clean state
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)
        self.QCBox_LayerForMasking.setCurrentIndex(-1)
        self.render_widget.canvas.setLayers([])
        self.render_widget.refresh()
        self.QCBox_LayerForMaskingBand.clear()
        # accept raster layers AND polygon vector layers as mask sources
        self.QCBox_LayerForMasking.setFilters(
            QgsMapLayerProxyModel.Filter.RasterLayer | QgsMapLayerProxyModel.Filter.PolygonLayer)
        # hide the thematic layer being edited so it can't be chosen as its own mask
        self.QCBox_LayerForMasking.setExceptedLayerList([LayerToEdit.current.qgs_layer])
        # dispatch when the user picks a layer from the combobox
        try: self.QCBox_LayerForMasking.layerChanged.disconnect()
        except TypeError: pass
        self.QCBox_LayerForMasking.layerChanged.connect(self.select_mask_layer)
        # refresh the classes table when the raster mask band changes
        try: self.QCBox_LayerForMaskingBand.currentIndexChanged.disconnect()
        except TypeError: pass
        self.QCBox_LayerForMaskingBand.currentIndexChanged.connect(self.setup_raster_mask_classes)
        # browse button: open a file dialog to load a mask (raster or polygon vector);
        # the layer is loaded hidden from the legend and removed on dialog close.
        try: self.QCBox_BrowseLayerForMasking.clicked.disconnect()
        except TypeError: pass
        self.QCBox_BrowseLayerForMasking.clicked.connect(lambda: browse_dialog_to_load_file(
            self, self.QCBox_LayerForMasking,
            dialog_title=self.tr("Select a raster or polygon vector file for masking"),
            file_filters=self.tr(
                "Raster or vector files (*.tif *.img *.shp *.gpkg *.geojson *.json *.kml *.gml);;"
                "Raster files (*.tif *.img);;"
                "Vector files (*.shp *.gpkg *.geojson *.json *.kml *.gml);;"
                "All files (*.*)"),
            msg_bar=self.MsgBar, add_to_legend=False))
        # toggle raster class selection via the table (raster mode only)
        try: self.PixelTable.itemClicked.disconnect()
        except TypeError: pass
        self.PixelTable.itemClicked.connect(self.table_item_clicked)
        # Apply button
        try: self.DialogButtons.button(QDialogButtonBox.StandardButton.Apply).clicked.disconnect()
        except TypeError: pass
        self.DialogButtons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(lambda: self.apply())
        # registry checkbox (tooltip reflects whether the registry is available)
        registry_enabled = LayerToEdit.current.registry.enabled if LayerToEdit.current else False
        self.RecordChangesInRegistry.setChecked(False)
        self.RecordChangesInRegistry.setEnabled(registry_enabled)
        tooltip_base = "<p>Add the changes that will be applied here to the ThRasE registry.</p>"
        if registry_enabled:
            tooltip = f"<html><head/><body>{tooltip_base}</body></html>"
        else:
            tooltip_notice = "<p><b>Registry is disabled:</b> enable it in the main dialog to store these edits.</p>"
            tooltip = f"<html><head/><body>{tooltip_base}{tooltip_notice}</body></html>"
        self.RecordChangesInRegistry.setToolTip(tooltip)
        # start with no layer selected
        self._set_mask_mode(None)

    def _set_mask_mode(self, mode):
        """Adjust dialog widgets for the current masking mode: 'raster', 'vector' or None.

        - raster / None : show the band selector and label the table as class picker.
        - vector        : hide the band selector and relabel the table as an
                          informational legend for the polygon mask.
        """
        is_vector = (mode == "vector")
        self.QCBox_LayerForMaskingBand.setVisible(not is_vector)
        self.label_3.setText(self.tr("Mask layer:") if is_vector else self.tr("Classes for masking:"))

    def restore_mask_symbology(self):
        """Restore the symbology of the active mask layer (raster or vector) to the original."""
        # restore raster mask symbology
        if self.raster_mask_layer and self.QCBox_LayerForMaskingBand.currentText() and self.pixel_classes_backup:
            band = int(self.QCBox_LayerForMaskingBand.currentText())
            symbology = \
                [(str(pixel["value"]), pixel["value"],
                  (pixel["color"]["R"], pixel["color"]["G"], pixel["color"]["B"], pixel["color"]["A"]))
                 for pixel in self.pixel_classes_backup]
            apply_symbology(self.raster_mask_layer, band, symbology)
        # restore vector mask renderer
        if self.vector_mask_layer and self.vector_mask_renderer_backup is not None:
            try:
                self.vector_mask_layer.setRenderer(self.vector_mask_renderer_backup.clone())
                self.vector_mask_layer.triggerRepaint()
            except RuntimeError:
                # layer may have been deleted meanwhile
                pass

    def _clear_mask_state(self):
        """Reset the canvas and internal mask state before switching layers."""
        self.restore_mask_symbology()
        self.raster_mask_layer = None
        self.vector_mask_layer = None
        self.vector_mask_renderer_backup = None
        self.pixel_classes = []
        self.pixel_classes_backup = []
        self.render_widget.canvas.setLayers([])
        self.render_widget.refresh()
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)
        with block_signals_to(self.QCBox_LayerForMaskingBand):
            self.QCBox_LayerForMaskingBand.clear()

    def _reset_layer_combo(self):
        """Clear the mask layer selection without re-triggering the signal chain."""
        with block_signals_to(self.QCBox_LayerForMasking):
            self.QCBox_LayerForMasking.setCurrentIndex(-1)

    def select_mask_layer(self, layer):
        """Handle a new mask layer selection: validate it and dispatch to the
        raster or vector setup depending on the layer type."""
        self._clear_mask_state()

        if not layer:
            self._set_mask_mode(None)
            return

        # CRS must match the layer to edit (applies to both raster and vector)
        if layer.crs() != LayerToEdit.current.qgs_layer.crs():
            self.MsgBar.pushMessage(
                f"The selected layer \"{layer.name()}\" doesn't have the same coordinate system "
                f"as the thematic layer to edit \"{LayerToEdit.current.qgs_layer.name()}\"",
                level=Qgis.MessageLevel.Critical, duration=20)
            self._reset_layer_combo()
            self._set_mask_mode(None)
            return

        if layer.type() == QgsMapLayer.LayerType.RasterLayer:
            self._setup_raster_mask(layer)
        elif layer.type() == QgsMapLayer.LayerType.VectorLayer:
            if layer.geometryType() != QgsWkbTypes.GeometryType.PolygonGeometry:
                self.MsgBar.pushMessage(
                    "Only polygon vector layers are supported as a mask",
                    level=Qgis.MessageLevel.Critical, duration=20)
                self._reset_layer_combo()
                self._set_mask_mode(None)
                return
            self._setup_vector_mask(layer)

    def _setup_raster_mask(self, layer):
        """Configure the dialog to use a raster layer as the mask source."""
        # pixel size must match (raster only)
        if (round(layer.rasterUnitsPerPixelX(), 3) != round(LayerToEdit.current.qgs_layer.rasterUnitsPerPixelX(), 3) or
            round(layer.rasterUnitsPerPixelY(), 3) != round(LayerToEdit.current.qgs_layer.rasterUnitsPerPixelY(), 3)):
            self.MsgBar.pushMessage(
                f"The selected raster \"{layer.name()}\" doesn't have the same pixel size "
                f"as the thematic layer to edit \"{LayerToEdit.current.qgs_layer.name()}\"",
                level=Qgis.MessageLevel.Critical, duration=20)
            self._reset_layer_combo()
            self._set_mask_mode(None)
            return

        self.render_widget.canvas.setDestinationCrs(layer.crs())
        self.render_widget.canvas.setLayers([layer])
        self.render_widget.canvas.setExtent(layer.extent())
        self.render_widget.refresh()
        self.raster_mask_layer = layer
        self._set_mask_mode("raster")

        # set band count
        with block_signals_to(self.QCBox_LayerForMaskingBand):
            self.QCBox_LayerForMaskingBand.clear()
            self.QCBox_LayerForMaskingBand.addItems([str(x) for x in range(1, self.raster_mask_layer.bandCount() + 1)])

        self.setup_raster_mask_classes()

    def _setup_vector_mask(self, layer):
        """Configure the dialog to use a polygon vector layer as the mask source."""
        # save the original renderer so we can restore it on close/apply
        current_renderer = layer.renderer()
        self.vector_mask_renderer_backup = current_renderer.clone() if current_renderer is not None else None

        # apply a yellow highlight symbology so the masked area is clearly visible
        yellow_symbol = QgsFillSymbol.createSimple({
            'color': ','.join(str(v) for v in VECTOR_MASK_FILL_RGBA),
            'style': 'solid',
            'outline_color': ','.join(str(v) for v in VECTOR_MASK_OUTLINE_RGBA),
            'outline_width': '0.5',
            'outline_style': 'solid',
        })
        layer.setRenderer(QgsSingleSymbolRenderer(yellow_symbol))
        layer.triggerRepaint()

        # show the layer to edit underneath and the vector mask on top
        self.render_widget.canvas.setDestinationCrs(layer.crs())
        self.render_widget.canvas.setLayers([layer, LayerToEdit.current.qgs_layer])
        self.render_widget.canvas.setExtent(layer.extent())
        self.render_widget.refresh()

        self.vector_mask_layer = layer
        self._set_mask_mode("vector")
        self.set_vector_mask_table()

    def set_vector_mask_table(self):
        """Populate the table with a single non-interactive row describing the vector mask."""
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)

        with block_signals_to(self.PixelTable):
            header = ["", ""]
            self.PixelTable.setRowCount(1)
            self.PixelTable.setColumnCount(2)
            self.PixelTable.horizontalHeader().setMinimumSectionSize(45)
            self.PixelTable.verticalHeader().setVisible(False)
            self.PixelTable.setHorizontalHeaderLabels(header)

            # color swatch (yellow, matching the polygon highlight)
            color_item = QTableWidgetItem()
            color_item.setFlags(color_item.flags()
                                & ~Qt.ItemFlag.ItemIsSelectable
                                & ~Qt.ItemFlag.ItemIsEditable)
            color_item.setBackground(QColor(*VECTOR_MASK_TABLE_RGBA))
            self.PixelTable.setItem(0, 0, color_item)

            # informative label (not selectable, not checkable)
            label_item = QTableWidgetItem(self.tr("mask area"))
            label_item.setFlags(label_item.flags()
                                & ~Qt.ItemFlag.ItemIsSelectable
                                & ~Qt.ItemFlag.ItemIsEditable)
            label_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            self.PixelTable.setItem(0, 1, label_item)

            self.PixelTable.resizeColumnsToContents()
            self.PixelTable.resizeRowsToContents()

    @pyqtSlot()
    @error_handler
    def setup_raster_mask_classes(self):
        """Extract pixel classes of the selected raster mask band and populate the classes table."""
        if not self.QCBox_LayerForMaskingBand.currentText() or self.raster_mask_layer is None:
            return
        band = int(self.QCBox_LayerForMaskingBand.currentText())
        xml_style_items = get_xml_style(self.raster_mask_layer, band)
        if xml_style_items is None:
            self.PixelTable.clear()
            self.PixelTable.setRowCount(0)
            self.PixelTable.setColumnCount(0)
            return
        self.pixel_classes = []
        for xml_item in xml_style_items:
            try:
                value = xml_item.get("value")
                color = xml_item.get("color")
                alpha = xml_item.get("alpha")

                if value is None or color is None or alpha is None:
                    continue

                pixel = {"value": int(value), "color": {}, "select": False}
                item_color = color.lstrip('#')
                item_color = tuple(int(item_color[i:i + 2], 16) for i in (0, 2, 4))
                pixel["color"]["R"] = item_color[0]
                pixel["color"]["G"] = item_color[1]
                pixel["color"]["B"] = item_color[2]
                pixel["color"]["A"] = int(alpha)
                self.pixel_classes.append(pixel)
            except (ValueError, AttributeError, TypeError):
                continue

        self.pixel_classes_backup = deepcopy(self.pixel_classes)

        self.set_pixel_classes_table()

    def set_pixel_classes_table(self):
        """Populate the table widget with the pixel classes of the raster mask."""
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)
        if not self.raster_mask_layer:
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
                        item_table.setFlags(item_table.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                        item_table.setBackground(QColor(pixel["color"]["R"], pixel["color"]["G"],
                                                        pixel["color"]["B"], pixel["color"]["A"]))
                        self.PixelTable.setItem(row_idx, col_idx, item_table)
                if header == "class value":
                    for row_idx, pixel in enumerate(self.pixel_classes):
                        item_table = QTableWidgetItem(str(pixel["value"]))
                        item_table.setFlags(item_table.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        item_table.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
                        self.PixelTable.setItem(row_idx, col_idx, item_table)
                if header == "select":
                    for row_idx, pixel in enumerate(self.pixel_classes):
                        item_table = QTableWidgetItem()
                        item_table.setFlags(item_table.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        item_table.setFlags(item_table.flags() | Qt.ItemFlag.ItemIsEnabled)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                        item_table.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
                        if pixel["select"]:
                            item_table.setCheckState(Qt.CheckState.Checked)
                        else:
                            item_table.setCheckState(Qt.CheckState.Unchecked)
                        self.PixelTable.setItem(row_idx, col_idx, item_table)

            # adjust size of Table
            self.PixelTable.resizeColumnsToContents()
            self.PixelTable.resizeRowsToContents()

    @pyqtSlot(QTableWidgetItem)
    def table_item_clicked(self, table_item):
        """Refresh the raster mask symbology when the user toggles a class checkbox:
        selected classes are painted yellow, deselected ones revert to their original color."""
        # only react to the "select" column (index 2)
        if table_item.column() != 2:
            return
        if not self.QCBox_LayerForMaskingBand.currentText() or self.raster_mask_layer is None:
            return

        band = int(self.QCBox_LayerForMaskingBand.currentText())
        row_idx = table_item.row()

        if table_item.checkState() == Qt.CheckState.Checked:
            r, g, b, a = VECTOR_MASK_TABLE_RGBA
            self.pixel_classes[row_idx]["color"] = {"R": r, "G": g, "B": b, "A": a}
        else:
            self.pixel_classes[row_idx]["color"] = self.pixel_classes_backup[row_idx]["color"]

        symbology = \
            [(str(pixel["value"]), pixel["value"],
              (pixel["color"]["R"], pixel["color"]["G"], pixel["color"]["B"], pixel["color"]["A"]))
             for pixel in self.pixel_classes]
        apply_symbology(self.raster_mask_layer, band, symbology)

    @staticmethod
    def _parse_vector_source(layer):
        """Return (path, layer_name) from a vector layer's source string.

        QGIS encodes multi-layer datasources (e.g. GeoPackage) as
        "<path>|layername=<name>|...". GDAL/OGR does not understand the "|layername="
        syntax directly, so extract the pieces here.
        """
        source = layer.source()
        if "|layername=" in source:
            path, _, params = source.partition("|")
            layer_name = None
            for param in params.split("|"):
                if param.startswith("layername="):
                    layer_name = param.split("=", 1)[1]
                    break
            return path, layer_name
        return source, None

    @wait_process
    def apply(self):
        """Apply the recode pixel table changes restricted to the selected mask
        (raster classes or vector polygons)."""

        from ThRasE.thrase import ThRasE

        if not (self.raster_mask_layer or self.vector_mask_layer):
            self.MsgBar.pushMessage(
                "Please select a raster or polygon vector layer to use as a mask",
                level=Qgis.MessageLevel.Warning, duration=10)
            return

        # raster masking requires at least one class selected in the table
        classes_selected = None
        if self.raster_mask_layer:
            pixel_table = self.PixelTable
            if pixel_table.rowCount() == 0:
                self.MsgBar.pushMessage("The pixel classes table is empty",
                                        level=Qgis.MessageLevel.Warning, duration=10)
                return
            classes_selected = [int(pixel_table.item(row_idx, 1).text()) for row_idx in range(len(self.pixel_classes))
                                if pixel_table.item(row_idx, 2).checkState() == Qt.CheckState.Checked]
            if not classes_selected:
                self.MsgBar.pushMessage("No class was selected to apply",
                                        level=Qgis.MessageLevel.Warning, duration=10)
                return

        mask_source_layer = self.raster_mask_layer or self.vector_mask_layer
        extent_intercepted = LayerToEdit.current.qgs_layer.extent().intersect(mask_source_layer.extent())
        if extent_intercepted.isEmpty():
            self.MsgBar.pushMessage("No overlap was found between the mask layer and the layer to edit",
                                    level=Qgis.MessageLevel.Info, duration=10)
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

            # Get georeferencing information of the layer to edit
            layer_gt = ds_in.GetGeoTransform()
            ps_x = layer_gt[1]           # pixel size in x
            ps_y = abs(layer_gt[5])      # pixel size in y (absolute value)

            # Calculate pixel indices in the layer-to-edit for the overlap extent
            x_min = extent_intercepted.xMinimum()
            x_max = extent_intercepted.xMaximum()
            y_min = extent_intercepted.yMinimum()
            y_max = extent_intercepted.yMaximum()

            # Indices in layer to edit
            layer_idx_x = int(round((x_min - layer_gt[0]) / ps_x))
            layer_idx_y = int(round((layer_gt[3] - y_max) / ps_y))
            cols = int(round((x_max - x_min) / ps_x))
            rows = int(round((y_max - y_min) / ps_y))

            if cols <= 0 or rows <= 0:
                self.MsgBar.pushMessage("No pixels were identified within the overlap extent",
                                        level=Qgis.MessageLevel.Info, duration=10)
                del ds_in
                return

            # Clamp to valid bounds for layer to edit
            layer_idx_x = max(0, layer_idx_x)
            layer_idx_y = max(0, layer_idx_y)
            cols = min(cols, ds_in.RasterXSize - layer_idx_x)
            rows = min(rows, ds_in.RasterYSize - layer_idx_y)

            if cols <= 0 or rows <= 0:
                self.MsgBar.pushMessage("No pixels to read within valid raster bounds",
                                        level=Qgis.MessageLevel.Info, duration=10)
                del ds_in
                return

            # ---- Build the boolean mask over the overlap sub-region ---- #
            if self.raster_mask_layer:
                mask = self._build_raster_classes_mask(
                    x_min, y_max, cols, rows, ps_x, ps_y, classes_selected)
            else:
                mask = self._build_vector_polygon_mask(
                    ds_in, layer_gt, layer_idx_x, layer_idx_y, cols, rows, ps_x, ps_y)

            if mask is None:
                del ds_in
                return
            if not mask.any():
                self.MsgBar.pushMessage(
                    "No pixels of the layer to edit fall within the selected mask in the overlap area",
                    level=Qgis.MessageLevel.Info, duration=10)
                del ds_in
                return

            # Extract the corresponding region from the layer to edit and apply recodes
            overlap_data = data_array[layer_idx_y:layer_idx_y + rows, layer_idx_x:layer_idx_x + cols]
            for old_value, new_value in LayerToEdit.current.old_new_value.items():
                change_mask = mask & (overlap_data == old_value)
                new_data_array[layer_idx_y:layer_idx_y + rows, layer_idx_x:layer_idx_x + cols][change_mask] = new_value

            # Compute which pixels actually changed
            row_indices, col_indices = np.nonzero(new_data_array != data_array)
            edited_pixels_count = int(row_indices.size)

            if edited_pixels_count == 0:
                self.MsgBar.pushMessage(
                    "No pixels were edited (the selected mask may not require changes in the target layer)",
                    level=Qgis.MessageLevel.Info, duration=10)
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

            del new_data_array, data_array, mask
            if old_values is not None:
                del old_values, new_values
            if row_indices is not None:
                del row_indices, col_indices

        except Exception as e:
            self.MsgBar.pushMessage(f"ERROR: {e}", level=Qgis.MessageLevel.Critical, duration=20)
            return

        if hasattr(LayerToEdit.current.qgs_layer, 'setCacheImage'):
            LayerToEdit.current.qgs_layer.setCacheImage(None)
        LayerToEdit.current.qgs_layer.reload()
        LayerToEdit.current.qgs_layer.triggerRepaint()

        ThRasE.dialog.editing_status.setText(f"{edited_pixels_count} pixels edited!")
        if record_changes and edited_pixels_count:
            ThRasE.dialog.registry_widget.update_registry()

        self.MsgBar.pushMessage(
            "DONE: Changes were successfully applied within the selected mask",
            level=Qgis.MessageLevel.Success, duration=10)

        # finish the edition
        self.restore_mask_symbology()
        # remove any layer hidden from the QGIS legend (e.g. loaded via the
        # browse button with `add_to_legend=False`) before we drop the references
        remove_layers_hidden_from_legend()
        # clear
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)
        self.render_widget.canvas.setLayers([])
        self.render_widget.canvas.clearCache()
        self.render_widget.refresh()
        self.QCBox_LayerForMasking.setCurrentIndex(-1)
        with block_signals_to(self.QCBox_LayerForMaskingBand):
            self.QCBox_LayerForMaskingBand.clear()

        self.accept()

    def _build_raster_classes_mask(self, x_min, y_max, cols, rows, ps_x, ps_y, classes_selected):
        """Return a boolean mask where selected classes in the raster mask are True."""
        classes_ds = gdal.Open(get_source_from(self.raster_mask_layer), gdal.GA_ReadOnly)
        if classes_ds is None:
            raise RuntimeError("Unable to open the raster mask file")
        try:
            classes_gt = classes_ds.GetGeoTransform()
            classes_idx_x = max(0, int(round((x_min - classes_gt[0]) / ps_x)))
            classes_idx_y = max(0, int(round((classes_gt[3] - y_max) / ps_y)))
            cols_c = min(cols, classes_ds.RasterXSize - classes_idx_x)
            rows_c = min(rows, classes_ds.RasterYSize - classes_idx_y)
            if cols_c <= 0 or rows_c <= 0:
                self.MsgBar.pushMessage("No pixels to read within valid raster bounds",
                                        level=Qgis.MessageLevel.Info, duration=10)
                return None

            raster_mask_band = int(self.QCBox_LayerForMaskingBand.currentText())
            class_array = classes_ds.GetRasterBand(raster_mask_band).ReadAsArray(
                classes_idx_x, classes_idx_y, cols_c, rows_c).astype(int)
        finally:
            del classes_ds

        mask = np.zeros((rows, cols), dtype=bool)
        mask[:class_array.shape[0], :class_array.shape[1]] = np.isin(class_array, classes_selected)
        return mask

    def _build_vector_polygon_mask(self, ds_in, layer_gt, layer_idx_x, layer_idx_y, cols, rows, ps_x, ps_y):
        """Rasterize the polygon vector mask over the layer-to-edit grid and return a boolean mask.

        Supports both file-backed vector layers (via a path that OGR can open) and
        QGIS memory/scratch layers (features are transferred into an OGR in-memory
        datasource before rasterization).

        Rasterization rule: GDAL's default (pixel-center rule). A pixel is included
        in the mask only when the polygon contains its center.
        """
        # align the in-memory mask raster to the layer-to-edit grid at the overlap sub-region
        overlap_x_min = layer_gt[0] + layer_idx_x * ps_x
        overlap_y_max = layer_gt[3] - layer_idx_y * ps_y

        mask_ds = gdal.GetDriverByName('MEM').Create('', cols, rows, 1, gdal.GDT_Byte)
        mask_ds.SetGeoTransform((overlap_x_min, ps_x, 0, overlap_y_max, 0, -ps_y))
        mask_ds.SetProjection(ds_in.GetProjection())

        vec_path, vec_layer_name = self._parse_vector_source(self.vector_mask_layer)
        is_file_backed = (self.vector_mask_layer.providerType() != "memory"
                          and bool(vec_path) and os.path.isfile(vec_path))

        if is_file_backed:
            rasterize_kwargs = {'burnValues': [1], 'allTouched': False}
            if vec_layer_name:
                rasterize_kwargs['layers'] = [vec_layer_name]
            if gdal.Rasterize(mask_ds, vec_path, **rasterize_kwargs) is None:
                raise RuntimeError(f"Unable to rasterize the vector mask \"{vec_path}\"")
        else:
            # non file-backed layers (memory, scratch, etc.) cannot be opened by
            # OGR via a path; transfer their polygons into an OGR in-memory
            # datasource and rasterize that instead
            ogr_ds = self._qgs_layer_to_ogr_memory(self.vector_mask_layer)
            ogr_layer = ogr_ds.GetLayer(0)
            if ogr_layer is None or ogr_layer.GetFeatureCount() == 0:
                raise RuntimeError("The selected vector mask has no polygon features")
            if gdal.RasterizeLayer(mask_ds, [1], ogr_layer, burn_values=[1],
                                   options=['ALL_TOUCHED=FALSE']) != 0:
                raise RuntimeError("Unable to rasterize the in-memory vector mask")

        return mask_ds.GetRasterBand(1).ReadAsArray().astype(bool)

    @staticmethod
    def _qgs_layer_to_ogr_memory(qgs_layer):
        """Copy polygon features of a QGIS vector layer into an OGR in-memory datasource.

        Used for memory/scratch layers that cannot be opened directly by GDAL/OGR
        through their source string.
        """
        srs = osr.SpatialReference()
        srs.ImportFromWkt(qgs_layer.crs().toWkt())

        mem_drv = ogr.GetDriverByName('Memory')
        mem_ds = mem_drv.CreateDataSource('thrase_vector_mask')
        mem_layer = mem_ds.CreateLayer('mask', srs, ogr.wkbMultiPolygon)
        layer_defn = mem_layer.GetLayerDefn()

        for feature in qgs_layer.getFeatures():
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue
            try:
                wkb = bytes(geom.asWkb())
            except Exception:
                continue
            ogr_geom = ogr.CreateGeometryFromWkb(wkb)
            if ogr_geom is None:
                continue
            ogr_feat = ogr.Feature(layer_defn)
            ogr_feat.SetGeometry(ogr_geom)
            mem_layer.CreateFeature(ogr_feat)
            ogr_feat = None

        return mem_ds
