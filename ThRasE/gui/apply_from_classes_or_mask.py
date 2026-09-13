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
from copy import deepcopy
from pathlib import Path

from qgis.core import Qgis, QgsFillSymbol, QgsProject, QgsSingleSymbolRenderer
from qgis.gui import QgsMapToolPan
from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt, pyqtSlot
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QTableWidgetItem

from ThRasE.core.editing import LayerToEdit
from ThRasE.core.raster_recode import RasterMaskSpec, RecodeRequest, VectorMaskSpec
from ThRasE.utils.others_utils import get_xml_style
from ThRasE.utils.qgis_utils import (
    apply_symbology,
    browse_dialog_to_load_file,
    get_source_from,
    global_edit_memory_budget,
)
from ThRasE.utils.system_utils import block_signals_to, error_handler

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, "ui", "apply_from_classes_or_mask.ui"))

# Yellow highlight used to mark the mask area on the canvas
VECTOR_MASK_FILL_RGBA = (255, 255, 0, 120)  # polygon fill on the canvas
VECTOR_MASK_OUTLINE_RGBA = (200, 160, 0, 255)  # polygon outline on the canvas
VECTOR_MASK_TABLE_RGBA = (255, 255, 0, 255)  # table vector mask (fully opaque)


class ApplyFromClassesOrMask(QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.setupUi(self)
        # raster mask state
        self.raster_mask_layer = None
        self.raster_mask_renderer_backup = None
        self.pixel_classes = []
        self.pixel_classes_backup = []
        # vector mask state
        self.vector_mask_layer = None
        self.vector_mask_renderer_backup = None
        self.owned_mask_layer_ids = set()

        self.map_tool_pan = QgsMapToolPan(self.render_widget.canvas)
        self.render_widget.canvas.setMapTool(self.map_tool_pan, clean=True)

    def reject(self):
        """Restore the mask layer symbology and remove the layers this dialog loaded.

        Only the layers browsed from here are removed (`owned_mask_layer_ids`), so a
        mask the user had already loaded in the project stays there.
        """
        self.cleanup_mask_state(remove_owned_layers=True)
        super().reject()

    def setup_gui(self):
        """Initialize the dialog: reset widgets, wire signals, accept both raster
        and polygon vector layers as mask sources."""
        # reset widgets to a clean state
        self.cleanup_mask_state(remove_owned_layers=True)
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)
        self.QCBox_LayerForMasking.setCurrentIndex(-1)
        self.render_widget.canvas.setLayers([])
        self.render_widget.refresh()
        self.QCBox_LayerForMaskingBand.clear()
        # accept raster layers AND polygon vector layers as mask sources
        self.QCBox_LayerForMasking.setFilters(Qgis.LayerFilter.RasterLayer | Qgis.LayerFilter.PolygonLayer)
        # hide the thematic layer being edited so it can't be chosen as its own mask
        self.QCBox_LayerForMasking.setExceptedLayerList([LayerToEdit.current.qgs_layer])
        # dispatch when the user picks a layer from the combobox
        try:
            self.QCBox_LayerForMasking.layerChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.QCBox_LayerForMasking.layerChanged.connect(self.select_mask_layer)
        # refresh the classes table when the raster mask band changes
        try:
            self.QCBox_LayerForMaskingBand.currentIndexChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.QCBox_LayerForMaskingBand.currentIndexChanged.connect(self.setup_raster_mask_classes)
        # browse button: open a file dialog to load a mask (raster or polygon vector);
        # the layer is loaded hidden from the legend and removed on dialog close.
        try:
            self.QCBox_BrowseLayerForMasking.clicked.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.QCBox_BrowseLayerForMasking.clicked.connect(self._browse_mask_layer)
        # toggle raster class selection via the table (raster mode only)
        try:
            self.PixelTable.itemClicked.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.PixelTable.itemClicked.connect(self.table_item_clicked)
        # Apply button
        try:
            self.DialogButtons.button(QDialogButtonBox.StandardButton.Apply).clicked.disconnect()
        except (TypeError, RuntimeError):
            pass
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
        is_vector = mode == "vector"
        self.QCBox_LayerForMaskingBand.setVisible(not is_vector)
        self.label_3.setText(self.tr("Mask layer:") if is_vector else self.tr("Classes for masking:"))

    def _browse_mask_layer(self):
        project = QgsProject.instance()
        layer_ids_before = set(project.mapLayers())
        layer = browse_dialog_to_load_file(
            self,
            self.QCBox_LayerForMasking,
            dialog_title=self.tr("Select a raster or polygon vector file for masking"),
            file_filters=self.tr(
                "Raster or vector files (*.tif *.img *.shp *.gpkg *.geojson *.json *.kml *.gml);;"
                "Raster files (*.tif *.img);;"
                "Vector files (*.shp *.gpkg *.geojson *.json *.kml *.gml);;"
                "All files (*.*)"
            ),
            msg_bar=self.MsgBar,
            add_to_legend=False,
        )
        if layer is not None and layer.id() not in layer_ids_before:
            self.owned_mask_layer_ids.add(layer.id())

    def cleanup_mask_state(self, remove_owned_layers=False):
        """Restore preview state and remove only layers loaded by this dialog."""
        self.restore_mask_symbology()
        self.render_widget.canvas.setLayers([])
        self.render_widget.canvas.clearCache()
        self.render_widget.refresh()
        self.raster_mask_layer = None
        self.raster_mask_renderer_backup = None
        self.vector_mask_layer = None
        self.vector_mask_renderer_backup = None
        self.pixel_classes = []
        self.pixel_classes_backup = []
        self.PixelTable.clear()
        self.PixelTable.setRowCount(0)
        self.PixelTable.setColumnCount(0)
        with block_signals_to(self.QCBox_LayerForMasking):
            self.QCBox_LayerForMasking.setCurrentIndex(-1)
        with block_signals_to(self.QCBox_LayerForMaskingBand):
            self.QCBox_LayerForMaskingBand.clear()
        if remove_owned_layers:
            project = QgsProject.instance()
            for layer_id in tuple(self.owned_mask_layer_ids):
                project.removeMapLayer(layer_id)
            self.owned_mask_layer_ids.clear()

    def restore_mask_symbology(self):
        """Restore the symbology of the active mask layer (raster or vector) to the original."""
        # restore raster mask symbology
        if self.raster_mask_layer and self.raster_mask_renderer_backup is not None:
            try:
                self.raster_mask_layer.setRenderer(self.raster_mask_renderer_backup.clone())
                self.raster_mask_layer.triggerRepaint()
            except RuntimeError:
                pass
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
        self.raster_mask_renderer_backup = None
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
                f'The selected layer "{layer.name()}" doesn\'t have the same coordinate system '
                f'as the thematic layer to edit "{LayerToEdit.current.qgs_layer.name()}"',
                level=Qgis.MessageLevel.Critical,
                duration=20,
            )
            self._reset_layer_combo()
            self._set_mask_mode(None)
            return

        if layer.type() == Qgis.LayerType.Raster:
            self._setup_raster_mask(layer)
        elif layer.type() == Qgis.LayerType.Vector:
            if layer.geometryType() != Qgis.GeometryType.Polygon:
                self.MsgBar.pushMessage(
                    "Only polygon vector layers are supported as a mask", level=Qgis.MessageLevel.Critical, duration=20
                )
                self._reset_layer_combo()
                self._set_mask_mode(None)
                return
            self._setup_vector_mask(layer)

    def _setup_raster_mask(self, layer):
        """Configure the dialog to use a raster layer as the mask source."""
        # pixel size must match (raster only)
        if round(layer.rasterUnitsPerPixelX(), 3) != round(
            LayerToEdit.current.qgs_layer.rasterUnitsPerPixelX(), 3
        ) or round(layer.rasterUnitsPerPixelY(), 3) != round(LayerToEdit.current.qgs_layer.rasterUnitsPerPixelY(), 3):
            self.MsgBar.pushMessage(
                f'The selected raster "{layer.name()}" doesn\'t have the same pixel size '
                f'as the thematic layer to edit "{LayerToEdit.current.qgs_layer.name()}"',
                level=Qgis.MessageLevel.Critical,
                duration=20,
            )
            self._reset_layer_combo()
            self._set_mask_mode(None)
            return

        self.render_widget.canvas.setDestinationCrs(layer.crs())
        self.render_widget.canvas.setLayers([layer])
        self.render_widget.canvas.setExtent(layer.extent())
        self.render_widget.refresh()
        self.raster_mask_layer = layer
        renderer = layer.renderer()
        self.raster_mask_renderer_backup = renderer.clone() if renderer is not None else None
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
        yellow_symbol = QgsFillSymbol.createSimple(
            {
                "color": ",".join(str(v) for v in VECTOR_MASK_FILL_RGBA),
                "style": "solid",
                "outline_color": ",".join(str(v) for v in VECTOR_MASK_OUTLINE_RGBA),
                "outline_width": "0.5",
                "outline_style": "solid",
            }
        )
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
            color_item.setFlags(color_item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEditable)
            color_item.setBackground(QColor(*VECTOR_MASK_TABLE_RGBA))
            self.PixelTable.setItem(0, 0, color_item)

            # informative label (not selectable, not checkable)
            label_item = QTableWidgetItem(self.tr("mask area"))
            label_item.setFlags(label_item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEditable)
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
                item_color = color.lstrip("#")
                item_color = tuple(int(item_color[i : i + 2], 16) for i in (0, 2, 4))
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
            for col_idx, col_header in enumerate(header):
                if col_header == "":
                    for row_idx, pixel in enumerate(self.pixel_classes):
                        item_table = QTableWidgetItem()
                        item_table.setFlags(item_table.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                        item_table.setBackground(
                            QColor(pixel["color"]["R"], pixel["color"]["G"], pixel["color"]["B"], pixel["color"]["A"])
                        )
                        self.PixelTable.setItem(row_idx, col_idx, item_table)
                if col_header == "class value":
                    for row_idx, pixel in enumerate(self.pixel_classes):
                        item_table = QTableWidgetItem(str(pixel["value"]))
                        item_table.setFlags(item_table.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        item_table.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
                        self.PixelTable.setItem(row_idx, col_idx, item_table)
                if col_header == "select":
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

        symbology = [
            (
                str(pixel["value"]),
                pixel["value"],
                (pixel["color"]["R"], pixel["color"]["G"], pixel["color"]["B"], pixel["color"]["A"]),
            )
            for pixel in self.pixel_classes
        ]
        apply_symbology(self.raster_mask_layer, band, symbology)

    def apply(self):
        """Schedule recoding restricted to the selected raster or vector mask."""

        from ThRasE.thrase import ThRasE

        if not (self.raster_mask_layer or self.vector_mask_layer):
            self.MsgBar.pushMessage(
                "Please select a raster or polygon vector layer to use as a mask",
                level=Qgis.MessageLevel.Warning,
                duration=10,
            )
            return

        # raster masking requires at least one class selected in the table
        classes_selected = None
        if self.raster_mask_layer:
            pixel_table = self.PixelTable
            if pixel_table.rowCount() == 0:
                self.MsgBar.pushMessage(
                    "The pixel classes table is empty", level=Qgis.MessageLevel.Warning, duration=10
                )
                return
            classes_selected = [
                int(pixel_table.item(row_idx, 1).text())
                for row_idx in range(len(self.pixel_classes))
                if pixel_table.item(row_idx, 2).checkState() == Qt.CheckState.Checked
            ]
            if not classes_selected:
                self.MsgBar.pushMessage("No class was selected to apply", level=Qgis.MessageLevel.Warning, duration=10)
                return

        mask_source_layer = self.raster_mask_layer or self.vector_mask_layer
        extent_intercepted = LayerToEdit.current.qgs_layer.extent().intersect(mask_source_layer.extent())
        if extent_intercepted.isEmpty():
            self.MsgBar.pushMessage(
                "No overlap was found between the mask layer and the layer to edit",
                level=Qgis.MessageLevel.Info,
                duration=10,
            )
            return

        record_changes = self.RecordChangesInRegistry.isChecked() and LayerToEdit.current.registry.enabled
        if self.raster_mask_layer:
            mask_spec = RasterMaskSpec(
                source_path=get_source_from(self.raster_mask_layer),
                band=int(self.QCBox_LayerForMaskingBand.currentText()),
                selected_values=tuple(classes_selected),
            )
        else:
            mask_spec = VectorMaskSpec(crs_wkt=self.vector_mask_layer.crs().toWkt())

        memory_budget_bytes = global_edit_memory_budget()
        layer_to_edit = LayerToEdit.current
        request = RecodeRequest(
            source_path=layer_to_edit.file_path,
            band=layer_to_edit.band,
            recode_pairs=tuple(layer_to_edit.old_new_value.items()),
            mask=mask_spec,
            collect_changes=record_changes,
            memory_budget_bytes=memory_budget_bytes,
        )

        def completed(_result):
            self.MsgBar.pushMessage(
                "DONE: Changes were successfully applied within the selected mask",
                level=Qgis.MessageLevel.Success,
                duration=10,
            )
            self.cleanup_mask_state(remove_owned_layers=True)
            self.accept()

        def no_changes(_result):
            self.MsgBar.pushMessage(
                "No pixels were edited (the selected mask may not require changes in the target layer)",
                level=Qgis.MessageLevel.Info,
                duration=10,
            )

        controller = getattr(ThRasE.dialog, "raster_recode_controller", None)
        if controller is None:
            self.MsgBar.pushMessage(
                "ERROR: The global raster task controller is not available",
                level=Qgis.MessageLevel.Critical,
                duration=20,
            )
            return
        controller.start(
            request,
            layer_to_edit,
            parent=self,
            source_dialog=self,
            on_success=completed,
            on_no_changes=no_changes,
            vector_mask_layer=self.vector_mask_layer,
        )
