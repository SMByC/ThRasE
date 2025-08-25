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
from pathlib import Path

from qgis.core import Qgis
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QWidget, QColorDialog, QFileDialog, QMessageBox
from qgis.PyQt.QtCore import pyqtSlot

from ThRasE.core.editing import LayerToEdit
from ThRasE.utils.system_utils import block_signals_to, wait_process

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'registry_widget.ui'))


class RegistryWidget(QWidget, FORM_CLASS):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.setupUi(self)
        self.setup_gui()
        self.total_pixels_modified = 0  # cache total pixels modified
        self.last_slider_position = 0

    def setup_gui(self):
        # initial state
        self.PixelLogGroups_Slider.setEnabled(False)
        self.PixelLogGroups_Slider.setToolTip("Registry of pixel groups modified during edit actions over time:\nEdit group 0 of 0")
        self.PixelLogGroup_DetailText.setText("")
        self.previousTileGroup.setEnabled(False)
        self.nextTileGroup.setEnabled(False)
        # hooks
        self.TilesColor.clicked.connect(self.change_tiles_color)
        self.previousTileGroup.clicked.connect(self.go_previous_group)
        self.nextTileGroup.clicked.connect(self.go_next_group)
        self.PixelLogGroups_Slider.valueChanged.connect(self.slider_changed)
        # show tiles while dragging the slider, not only on release
        self.PixelLogGroups_Slider.sliderMoved.connect(self.change_group_from_slider)
        self.PixelLogGroups_Slider.sliderReleased.connect(lambda: self.change_group_from_slider(self.PixelLogGroups_Slider.value()))
        # auto center button
        self.autoCenter.clicked.connect(self.center_to_current_group)
        # show all button
        self.showAll.setChecked(False)
        self.showAll.toggled.connect(self.toggle_show_all)
        # export registry
        self.QPBtn_ExportRegistry.clicked.connect(self.export_registry)
        self.QPBtn_ExportRegistry.setEnabled(False)
        # delete registry
        self.DeleteRegistry.clicked.connect(self.delete_registry)
        # enable/disable registry
        self.EnableRegistry.toggled.connect(self.toggle_registry_enabled)

    def update_registry(self, go_to_last=True):
        # only process when the widget is visible and enabled
        if not self.isVisible() or not LayerToEdit.current.registry.enabled:
            return
        if not LayerToEdit.current:
            self.set_empty_state()
            return
        status = LayerToEdit.current.registry.update()
        total_groups = len(LayerToEdit.current.registry.groups)
        # compute total modified pixels once to avoid repeated sums during slider moves
        self.total_pixels_modified = sum(len(g.tiles) for g in LayerToEdit.current.registry.groups)
        # toggle registry export button
        self.QPBtn_ExportRegistry.setEnabled(self.total_pixels_modified > 0)
        if not status or total_groups == 0:
            self.set_empty_state()
            return
        # enable and set controls
        with block_signals_to(self.PixelLogGroups_Slider):
            self.PixelLogGroups_Slider.setEnabled(True)
            self.PixelLogGroups_Slider.setMaximum(total_groups)
            # go to latest registry tile group
            if go_to_last:
                self.PixelLogGroups_Slider.setValue(total_groups)
                self.change_group_from_slider(total_groups)
            else:
                self.PixelLogGroups_Slider.setValue(self.last_slider_position)
                self.change_group_from_slider(self.last_slider_position)
        # enable show-all toggle and repaint if needed
        self.showAll.setEnabled(True)
        if self.showAll.isChecked():
            LayerToEdit.current.registry.show_all()

    @wait_process
    def showEvent(self, event):
        # when the widget becomes visible
        super().showEvent(event)
        self.update_registry(go_to_last=False)

    def hideEvent(self, event):
        # when the widget is hidden, clear tiles from canvases
        if LayerToEdit.current:
            LayerToEdit.current.registry.clear()
            LayerToEdit.current.registry.clear_show_all()
        self.last_slider_position = self.PixelLogGroups_Slider.value()
        super().hideEvent(event)

    def set_empty_state(self):
        self.PixelLogGroups_Slider.setEnabled(False)
        self.PixelLogGroups_Slider.setToolTip("Registry of pixel groups modified during edit actions over time:\nEdit group 0 of 0")
        self.PixelLogGroup_DetailText.setText("No modified pixels found in the registry")
        self.previousTileGroup.setEnabled(False)
        self.nextTileGroup.setEnabled(False)
        # reset cached counters
        self.total_pixels_modified = 0
        # show-all toggler reset
        self.showAll.setEnabled(False)
        self.showAll.setChecked(False)
        if LayerToEdit.current:
            LayerToEdit.current.registry.clear_show_all()
        # disable export when empty
        self.QPBtn_ExportRegistry.setEnabled(False)

    @pyqtSlot()
    def change_tiles_color(self, color=None):
        if not LayerToEdit.current:
            return
        if not color:
            color = QColorDialog.getColor(LayerToEdit.current.registry.tiles_color, self)
        if color.isValid():
            # update the registry color
            LayerToEdit.current.registry.tiles_color = color
            self.TilesColor.setStyleSheet("QToolButton{{background-color:{};}}".format(color.name()))
            
            # update color for all existing groups without rebuilding
            for group in LayerToEdit.current.registry.groups:
                group.tile_color = color
            
            # update color of current group without rebuilding geometry
            LayerToEdit.current.registry.update_current_group_color()
            # recolor show-all overlays if visible
            LayerToEdit.current.registry.update_show_all_color()

    @pyqtSlot(int)
    def change_group_from_slider(self, idx_group):
        if not LayerToEdit.current or not LayerToEdit.current.registry.groups:
            self.set_empty_state()
            return
        registry = LayerToEdit.current.registry
        registry.set_current_group(idx_group)
        total_groups = len(registry.groups)
        self.PixelLogGroups_Slider.setToolTip("Registry of pixel groups modified during edit actions over time:\nEdit group {} of {}".format(idx_group, total_groups))
        group = registry.current_group
        self.PixelLogGroup_DetailText.setText(
            "{} pixels modified at {} | Total: {} pixels modified".format(
                len(group.tiles), group.edit_date.strftime('%I:%M %p, %d-%m-%Y'), self.total_pixels_modified)
        )
        # enable/disable nav buttons
        self.previousTileGroup.setEnabled(idx_group > 1)
        self.nextTileGroup.setEnabled(idx_group < total_groups)

    def slider_changed(self, idx_group):
        total_groups = len(LayerToEdit.current.registry.groups) if LayerToEdit.current else 0
        self.PixelLogGroups_Slider.setToolTip("Registry of pixel groups modified during edit actions over time:\nEdit group {} of {}".format(idx_group, total_groups))

    @pyqtSlot()
    def go_previous_group(self):
        if not LayerToEdit.current or not LayerToEdit.current.registry.current_group:
            return
        idx_group = LayerToEdit.current.registry.current_group.idx
        if idx_group <= 1:
            return
        self.PixelLogGroups_Slider.setValue(idx_group - 1)
        self.change_group_from_slider(idx_group - 1)

    @pyqtSlot()
    def go_next_group(self):
        if not LayerToEdit.current or not LayerToEdit.current.registry.current_group:
            return
        total = len(LayerToEdit.current.registry.groups)
        idx_group = LayerToEdit.current.registry.current_group.idx
        if idx_group >= total:
            return
        self.PixelLogGroups_Slider.setValue(idx_group + 1)
        self.change_group_from_slider(idx_group + 1)

    @pyqtSlot()
    def center_to_current_group(self):
        if not LayerToEdit.current or not LayerToEdit.current.registry.current_group:
            return
        if self.autoCenter.isChecked():
            LayerToEdit.current.registry.current_group.center()

    @pyqtSlot(bool)
    def toggle_show_all(self, checked):
        if not LayerToEdit.current or not LayerToEdit.current.registry.enabled:
            return
        if checked:
            LayerToEdit.current.registry.show_all()
        else:
            LayerToEdit.current.registry.clear_show_all()

    @pyqtSlot(bool)
    def toggle_registry_enabled(self, enabled):
        if not LayerToEdit.current:
            return
        LayerToEdit.current.registry.enabled = enabled
        # restore or clear drawings
        if enabled and self.isVisible():
            if LayerToEdit.current.registry.groups:
                idx = self.PixelLogGroups_Slider.value()
                self.change_group_from_slider(idx)
            if self.showAll.isChecked():
                LayerToEdit.current.registry.show_all()
        else:
            LayerToEdit.current.registry.clear()
            LayerToEdit.current.registry.clear_show_all()
        # update controls state
        self.PixelLogGroups_Slider.setEnabled(enabled and self.total_pixels_modified > 0)
        self.previousTileGroup.setEnabled(enabled and self.PixelLogGroups_Slider.value() > 1)
        self.nextTileGroup.setEnabled(enabled and self.PixelLogGroups_Slider.value() < len(LayerToEdit.current.registry.groups))
        self.showAll.setEnabled(enabled)
        self.autoCenter.setEnabled(enabled)
        self.TilesColor.setEnabled(enabled)
        self.DeleteRegistry.setEnabled(enabled)
        self.QPBtn_ExportRegistry.setEnabled(enabled and self.total_pixels_modified > 0)
        self.PixelLogGroup_DetailText.setEnabled(enabled)

    @pyqtSlot()
    def export_registry(self):
        from ThRasE.thrase import ThRasE
        if not LayerToEdit.current:
            return
        # suggest filename near the raster
        layer_path = LayerToEdit.current.file_path or ""
        base_dir = os.path.dirname(layer_path) if os.path.isdir(os.path.dirname(layer_path)) else os.path.expanduser("~")
        base_name = os.path.splitext(os.path.basename(layer_path))[0] or "thrase"
        suggested = os.path.join(base_dir, f"{base_name}_pixel_registry.gpkg")

        output_file, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export pixel registry to vector file"),
            suggested,
            self.tr("GeoPackage (*.gpkg);;Shapefile (*.shp);;GeoJSON (*.geojson);;All files (*.*)"),
        )
        if not output_file:
            return

        ok, msg, count = LayerToEdit.current.registry.export_pixel_logs(output_file)

        if ok:
            ThRasE.dialog.MsgBar.pushMessage(self.tr(f"Exported {count} edited pixels to {output_file}"), level=Qgis.Success, duration=5)
        else:
            ThRasE.dialog.MsgBar.pushMessage(self.tr(f"Export failed: {msg}"), level=Qgis.Critical, duration=10)

    @pyqtSlot()
    def delete_registry(self):
        from ThRasE.thrase import ThRasE
        if not LayerToEdit.current:
            return
        # confirm action to delete entire registry
        reply = QMessageBox.warning(
            self,
            self.tr("Delete registry"),
            self.tr("Are you sure you want to delete the entire registry for this layer? "
                    "This action does not undo the changes made in the layer."),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        # delete all registry entries for the current layer
        LayerToEdit.current.pixel_log_store = {}
        LayerToEdit.current.registry.delete()
        self.set_empty_state()
        ThRasE.dialog.MsgBar.pushMessage(self.tr("Registry cleared"), level=Qgis.Success, duration=5)
