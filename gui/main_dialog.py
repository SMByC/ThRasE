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

import os
import configparser
from copy import deepcopy
from pathlib import Path

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import pyqtSignal, Qt, pyqtSlot
from qgis.PyQt.QtWidgets import QMessageBox, QGridLayout, QFileDialog, QTableWidgetItem, QColorDialog
from qgis.core import Qgis, QgsMapLayer, QgsMapLayerProxyModel
from qgis.PyQt.QtGui import QColor, QFont

from ThRasE.core.edition import LayerToEdit
from ThRasE.gui.about_dialog import AboutDialog
from ThRasE.gui.view_widget import ViewWidget
from ThRasE.utils.qgis_utils import load_and_select_filepath_in, valid_file_selected_in, apply_symbology

# plugin path
from ThRasE.utils.system_utils import block_signals_to

plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'main_dialog.ui'))

cfg = configparser.ConfigParser()
cfg.read(str(Path(plugin_folder, 'metadata.txt')))
VERSION = cfg.get('general', 'version')
HOMEPAGE = cfg.get('general', 'homepage')


class ThRasEDialog(QtWidgets.QDialog, FORM_CLASS):
    closingPlugin = pyqtSignal()
    view_widgets = []

    def __init__(self, parent=None):
        """Constructor."""
        super(ThRasEDialog, self).__init__(parent)
        # Set up the user interface from Designer through FORM_CLASS.
        # After self.setupUi() you can access any designer object by doing
        # self.<objectname>, and you can use autoconnect slots - see
        # http://qt-project.org/doc/qt-4.8/designer-using-a-ui-file.html
        # #widgets-and-dialogs-with-auto-connect
        self.setupUi(self)
        self.setup_gui()

    def setup_gui(self):
        # ######### plugin info ######### #
        self.about_dialog = AboutDialog()
        self.QPBtn_PluginInfo.setText("v{}".format(VERSION))
        self.QPBtn_PluginInfo.clicked.connect(self.about_dialog.show)

        # ######### navigation ######### #
        self.NavigationBlockWidget.setHidden(True)
        self.NavigationBlockWidget.setDisabled(True)
        self.NavigationBlockWidgetControls.setDisabled(True)
        self.QCBox_NavType.currentIndexChanged[str].connect(self.set_navigation_tool)
        self.QPBtn_BuildNavigationDialog.clicked.connect(self.open_build_navigation_dialog)
        self.QPBtn_ReloadRecodeTable.setDisabled(True)
        self.QPBtn_RestoreRecodeTable.setDisabled(True)
        self.Widget_GlobalEditTools.setDisabled(True)
        self.currentTile.clicked.connect(self.go_to_current_tile)
        self.previousTile.clicked.connect(self.go_to_previous_tile)
        self.nextTile.clicked.connect(self.go_to_next_tile)
        self.currentTileKeepVisible.clicked.connect(self.current_tile_keep_visible)

        # ######### build the view render widgets windows ######### #
        render_view_config = RenderViewConfig()
        if render_view_config.exec_():
            grid_rows = render_view_config.grid_rows.value()
            grid_columns = render_view_config.grid_columns.value()
        else:
            # by default
            grid_rows = 1
            grid_columns = 1

        # configure the views layout
        views_layout = QGridLayout()
        views_layout.setSpacing(0)
        views_layout.setMargin(0)
        view_widgets = []
        for row in range(grid_rows):
            for column in range(grid_columns):
                new_view_widget = ViewWidget()
                views_layout.addWidget(new_view_widget, row, column)
                view_widgets.append(new_view_widget)

        # add to change analysis dialog
        self.widget_view_windows.setLayout(views_layout)
        # save instances
        ThRasEDialog.view_widgets = view_widgets
        # setup view widget
        for idx, view_widget in enumerate(ThRasEDialog.view_widgets, start=1):
            view_widget.id = idx
            view_widget.setup_view_widget()

        # ######### setup layer to edit ######### #
        self.QCBox_LayerToEdit.setCurrentIndex(-1)
        self.QCBox_LayerToEdit.setFilters(QgsMapLayerProxyModel.RasterLayer)
        # handle connect layer selection
        self.QCBox_LayerToEdit.layerChanged.connect(self.select_layer_to_edit)
        self.QCBox_band_LayerToEdit.currentIndexChanged.connect(self.setup_layer_to_edit)
        # call to browse the render file
        self.QPBtn_browseLayerToEdit.clicked.connect(self.browse_dialog_layer_to_edit)
        # update recode pixel table
        self.recodePixelTable.itemChanged.connect(self.update_recode_pixel_table)
        # for change the class color
        self.recodePixelTable.itemClicked.connect(self.table_item_clicked)

        # ######### others ######### #
        self.QPBtn_ReloadRecodeTable.clicked.connect(self.reload_recode_table)
        self.QPBtn_RestoreRecodeTable.clicked.connect(self.restore_recode_table)
        self.QGBox_GlobalEditTools.setHidden(True)
        self.QPBtn_ApplyWholeImage.clicked.connect(self.apply_whole_image)
        #self.QPBtn_ApplyUsingExternalClass.clicked.connect(self.apply_using_external_class) TODO
        self.QPBtn_ApplyUsingExternalClass.setDisabled(True)

    def keyPressEvent(self, event):
        # ignore esc key for close the main dialog
        if not event.key() == Qt.Key_Escape:
            super(ThRasEDialog, self).keyPressEvent(event)

    def closeEvent(self, event):
        # first prompt
        quit_msg = "Are you sure you want close the ThRasE plugin?"
        reply = QMessageBox.question(None, 'Closing the ThRasE plugin',
                                     quit_msg, QMessageBox.Yes, QMessageBox.No)
        if reply == QMessageBox.No:
            # don't close
            event.ignore()
            return
        # close
        self.closingPlugin.emit()
        event.accept()

    @pyqtSlot()
    def browse_dialog_layer_to_edit(self):
        file_path, _ = QFileDialog.getOpenFileName(self,
            self.tr("Select the thematic raster file to edit"), "",
            self.tr("Raster files (*.tif *.img);;All files (*.*)"))
        if file_path != '' and os.path.isfile(file_path):
            # load to qgis and update combobox list
            load_and_select_filepath_in(self.QCBox_RenderFile_1, file_path, "raster")

    def set_navigation_tool(self, nav_type):
        if nav_type == "free":
            self.NavigationBlockWidget.setHidden(True)
        if nav_type == "by tiles":
            self.NavigationBlockWidget.setVisible(True)

    @pyqtSlot()
    def go_to_current_tile(self):
        if LayerToEdit.current.navigation.is_valid:
            LayerToEdit.current.navigation.current_tile.focus()

    @pyqtSlot()
    def go_to_previous_tile(self):
        if LayerToEdit.current.navigation.is_valid:
            # set the previous tile
            LayerToEdit.current.navigation.previous_tile()
            # adjust navigation components
            self.QPBar_TilesNavigation.setValue(LayerToEdit.current.navigation.current_tile.idx + 1)
            self.nextTile.setEnabled(True)
            if LayerToEdit.current.navigation.current_tile.idx == 0:  # first item
                self.previousTile.setEnabled(False)

    @pyqtSlot()
    def go_to_next_tile(self):
        if LayerToEdit.current.navigation.is_valid:
            # set the next tile
            LayerToEdit.current.navigation.next_tile()
            # adjust navigation components
            self.QPBar_TilesNavigation.setValue(LayerToEdit.current.navigation.current_tile.idx + 1)
            self.previousTile.setEnabled(True)
            if LayerToEdit.current.navigation.current_tile.idx + 1 == len(LayerToEdit.current.navigation.tiles):  # last item
                self.nextTile.setEnabled(False)

    @pyqtSlot()
    def current_tile_keep_visible(self):
        if self.currentTileKeepVisible.isChecked():
            [instance.show() for instance in LayerToEdit.current.navigation.current_tile.instances]
        else:
            [instance.hide() for instance in LayerToEdit.current.navigation.current_tile.instances]

        [view_widget.render_widget.canvas.refresh() for view_widget in ThRasEDialog.view_widgets if view_widget.is_active]

    @pyqtSlot()
    def open_build_navigation_dialog(self):
        LayerToEdit.current.build_navigation_dialog.exec_()

        if LayerToEdit.current.navigation.is_valid:
            # init and set the progress bar and navigation
            self.NavigationBlockWidgetControls.setEnabled(True)
            self.QPBar_TilesNavigation.setMaximum(len(LayerToEdit.current.navigation.tiles))
            self.QPBar_TilesNavigation.setValue(LayerToEdit.current.navigation.current_tile.idx + 1)
            if LayerToEdit.current.navigation.current_tile.idx == 0:
                self.previousTile.setEnabled(False)
                LayerToEdit.current.navigation.current_tile.show()
                LayerToEdit.current.navigation.current_tile.focus()
                [view_widget.render_widget.canvas.refresh() for view_widget in ThRasEDialog.view_widgets
                 if view_widget.is_active]
        else:
            self.NavigationBlockWidgetControls.setEnabled(False)

    @pyqtSlot(QgsMapLayer)
    def select_layer_to_edit(self, layer_selected):
        # first clear table
        self.recodePixelTable.setRowCount(0)
        self.recodePixelTable.setColumnCount(0)

        # disable and clear if layer selected is wrong
        def disable():
            self.NavigationBlockWidget.setDisabled(True)
            self.QPBtn_ReloadRecodeTable.setDisabled(True)
            self.QPBtn_RestoreRecodeTable.setDisabled(True)
            self.Widget_GlobalEditTools.setDisabled(True)
            self.QCBox_LayerToEdit.setCurrentIndex(-1)
            with block_signals_to(self.QCBox_band_LayerToEdit):
                self.QCBox_band_LayerToEdit.clear()
            LayerToEdit.current = None
            [view_widget.widget_EditionTools.setEnabled(False) for view_widget in ThRasEDialog.view_widgets]

        # first check
        if layer_selected is None:
            disable()
            return
        if not valid_file_selected_in(self.QCBox_LayerToEdit):
            self.MsgBar.pushMessage("The thematic raster layer to edit is not valid", level=Qgis.Warning)
            disable()
            return
        # show warning for layer to edit different to tif format
        if layer_selected.dataProvider().dataSourceUri()[-3::].lower() != "tif":
            quit_msg = "Use raster files different to GTiff format has not been fully tested. " \
                       "GTiff files are recommended for editing.\n\n" \
                       "Do you want to continue anyway?"
            reply = QMessageBox.question(None, 'Image to edit in ThRasE',
                                         quit_msg, QMessageBox.Yes, QMessageBox.No)
            if reply == QMessageBox.No:
                disable()
                return

        # check if thematic layer to edit has data type as integer or byte
        if layer_selected.dataProvider().dataType(1) not in [1, 2, 3, 4, 5]:
            self.MsgBar.pushMessage("The thematic raster layer to edit must be byte or integer as data type",
                                    level=Qgis.Warning)
            disable()
            return
        # set band count
        with block_signals_to(self.QCBox_band_LayerToEdit):
            self.QCBox_band_LayerToEdit.clear()
            self.QCBox_band_LayerToEdit.addItems([str(x) for x in range(1, layer_selected.bandCount() + 1)])

        self.setup_layer_to_edit()

    def setup_layer_to_edit(self):
        layer = self.QCBox_LayerToEdit.currentLayer()
        band = int(self.QCBox_band_LayerToEdit.currentText())

        if (layer.id(), band) in LayerToEdit.instances:
            layer_to_edit = LayerToEdit.instances[(layer.id(), band)]
        else:
            # create new instance
            layer_to_edit = LayerToEdit(layer, band)
            # init data for recode pixel table
            recode_pixel_table_status = layer_to_edit.setup_pixel_table()
            if recode_pixel_table_status is False:  # wrong style for set the recode pixel table
                del LayerToEdit.instances[(layer_to_edit.qgs_layer.id(), layer_to_edit.band)]
                self.QCBox_LayerToEdit.setCurrentIndex(-1)
                with block_signals_to(self.QCBox_band_LayerToEdit):
                    self.QCBox_band_LayerToEdit.clear()
                return

        LayerToEdit.current = layer_to_edit

        # set the CRS of all canvas view based on current thematic layer to edit
        [view_widget.render_widget.set_crs(layer_to_edit.qgs_layer.crs()) for view_widget in ThRasEDialog.view_widgets]
        # create the recode table
        self.set_recode_pixel_table()
        # enable some components
        self.NavigationBlockWidget.setEnabled(True)
        [view_widget.widget_EditionTools.setEnabled(True) for view_widget in ThRasEDialog.view_widgets]
        self.QPBtn_ReloadRecodeTable.setEnabled(True)
        self.QPBtn_RestoreRecodeTable.setEnabled(True)
        self.Widget_GlobalEditTools.setEnabled(True)

    def update_recode_pixel_table(self):
        layer_to_edit = LayerToEdit.current
        layer_to_edit.old_new_value = {}
        if not layer_to_edit or layer_to_edit.pixels is None:
            return

        for row_idx, pixel in enumerate(layer_to_edit.pixels):
            # assign the new value
            new_value = self.recodePixelTable.item(row_idx, 2).text()
            try:
                if new_value == "":
                    pixel["new_value"] = None
                elif float(new_value) == int(new_value):
                    pixel["new_value"] = int(new_value)
                    layer_to_edit.old_new_value[pixel["value"]] = pixel["new_value"]
            except:
                pass
            # assign the on state
            on = self.recodePixelTable.item(row_idx, 3)
            if on.checkState() == 2:
                pixel["s/h"] = True
            if on.checkState() == 0:
                pixel["s/h"] = False

        # update pixel class visibility
        pixel_class_visibility = [255 if self.recodePixelTable.item(row_idx, 3).checkState() == 2 else 0
                                  for row_idx in range(len(layer_to_edit.pixels))]
        layer_to_edit.symbology = [(row[0], row[1], (row[2][0], row[2][1], row[2][2], pcv))
                                   for row, pcv in zip(layer_to_edit.symbology, pixel_class_visibility)]
        apply_symbology(layer_to_edit.qgs_layer, layer_to_edit.band, layer_to_edit.symbology)

        # update table
        self.set_recode_pixel_table()

    def set_recode_pixel_table(self):
        layer_to_edit = LayerToEdit.current

        if not layer_to_edit or layer_to_edit.pixels is None:
            # clear table
            self.recodePixelTable.clear()
            return

        with block_signals_to(self.recodePixelTable):
            header = ["", "old value", "new value", "s/h"]
            row_length = len(layer_to_edit.pixels)
            # init table
            self.recodePixelTable.setRowCount(row_length)
            self.recodePixelTable.setColumnCount(4)
            self.recodePixelTable.horizontalHeader().setMinimumSectionSize(45)
            # hidden row labels
            self.recodePixelTable.verticalHeader().setVisible(False)
            # add Header
            self.recodePixelTable.setHorizontalHeaderLabels(header)
            # insert items
            for col_idx, header in enumerate(header):
                if header == "":
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem()
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setToolTip("Color for this class, click to edit.\n"
                                              "Info: edit the color is only temporarily")
                        item_table.setBackground(QColor(pixel["color"]["R"], pixel["color"]["G"],
                                                        pixel["color"]["B"], pixel["color"]["A"]))
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)
                if header == "old value":
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem(str(pixel["value"]))
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsEditable)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        item_table.setToolTip("The current value for this class")
                        if pixel["new_value"] is not None and pixel["new_value"] != pixel["value"]:
                            font = QFont()
                            font.setBold(True)
                            item_table.setFont(font)
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)
                if header == "new value":
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem(str(pixel["new_value"]) if pixel["new_value"] is not None else "")
                        item_table.setFlags(item_table.flags() | Qt.ItemIsEnabled | Qt.ItemIsEditable)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        item_table.setToolTip("Set the new value for this class")
                        if pixel["new_value"] is not None and pixel["new_value"] != pixel["value"]:
                            font = QFont()
                            font.setBold(True)
                            item_table.setFont(font)
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)
                if header == "s/h":
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem()
                        item_table.setFlags(item_table.flags() | Qt.ItemIsUserCheckable)
                        item_table.setFlags(item_table.flags() | Qt.ItemIsEnabled)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        item_table.setToolTip("Show/hide the pixel class value. \nIf it is "
                                              "hidden it does not avoid being edited!")
                        if pixel["s/h"]:
                            item_table.setCheckState(Qt.Checked)
                        else:
                            item_table.setCheckState(Qt.Unchecked)
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)

            # adjust size of Table
            self.recodePixelTable.resizeColumnsToContents()
            self.recodePixelTable.resizeRowsToContents()
            # adjust the editor block based on table content
            table_width = self.recodePixelTable.horizontalHeader().length() + 40
            self.EditionBlock.setMaximumWidth(table_width)
            self.EditionBlock.setMinimumWidth(table_width)

    def table_item_clicked(self, table_item):
        if table_item.text() == "none":
            return
        # set color
        if table_item.column() == 0:
            remember_color = table_item.background().color()
            color = QColorDialog.getColor(remember_color, self)
            if color.isValid():
                # update recode table
                with block_signals_to(self.recodePixelTable):
                    self.recodePixelTable.item(table_item.row(), 0).setBackground(color)
                # update pixels variable
                LayerToEdit.current.pixels[table_item.row()]["color"] = \
                    {"R": color.red(), "G": color.green(), "B": color.blue(), "A": color.alpha()}
                # apply to layer
                LayerToEdit.current.symbology[table_item.row()] = \
                    LayerToEdit.current.symbology[table_item.row()][0:2] + \
                    ((color.red(), color.green(), color.blue(), color.alpha()),)
                self.update_recode_pixel_table()

    def reload_recode_table(self):
        old_pixels = LayerToEdit.current.pixels
        pixels_backup = LayerToEdit.current.pixels_backup
        recode_pixel_table_status = LayerToEdit.current.setup_pixel_table(force_update=True)

        if recode_pixel_table_status is False:  # wrong style for set the recode pixel table
            self.recodePixelTable.clear()
            self.recodePixelTable.setRowCount(0)
            self.recodePixelTable.setColumnCount(0)
            # disable some components
            self.NavigationBlockWidget.setEnabled(False)
            [view_widget.widget_EditionTools.setEnabled(False) for view_widget in ThRasEDialog.view_widgets]
            self.Widget_GlobalEditTools.setEnabled(False)
            return
        # restore backup
        LayerToEdit.current.pixels_backup = pixels_backup
        # restore new pixel values and visibility
        for new_item in LayerToEdit.current.pixels:
            if new_item["value"] in [i["value"] for i in old_pixels]:
                old_item = next((i for i in old_pixels if i["value"] == new_item["value"]))
                new_item["new_value"] = old_item["new_value"]
                new_item["s/h"] = old_item["s/h"]
        self.setup_layer_to_edit()
        self.update_recode_pixel_table()

    def restore_recode_table(self):
        # restore the pixels and symbology variables
        LayerToEdit.current.pixels = deepcopy(LayerToEdit.current.pixels_backup)
        LayerToEdit.current.setup_symbology()
        # restore table
        self.set_recode_pixel_table()
        # update pixel class visibility
        apply_symbology(LayerToEdit.current.qgs_layer, LayerToEdit.current.band, LayerToEdit.current.symbology)
        # enable some components
        self.NavigationBlockWidget.setEnabled(True)
        [view_widget.widget_EditionTools.setEnabled(True) for view_widget in ThRasEDialog.view_widgets]
        self.Widget_GlobalEditTools.setEnabled(True)

    def apply_whole_image(self):
        # first prompt
        quit_msg = "This action apply the changes set in recode pixels table to the whole image, this cannot undone. \n\n" \
                   "Layer to apply: \"{}\"".format(LayerToEdit.current.qgs_layer.name())
        reply = QMessageBox.question(None, 'Applying changes to the whole image',
                                     quit_msg, QMessageBox.Ok, QMessageBox.Cancel)
        if reply == QMessageBox.Ok:
            LayerToEdit.current.edit_whole_image()


FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'render_view_config_dialog.ui'))


class RenderViewConfig(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self):
        QtWidgets.QDialog.__init__(self)
        self.setupUi(self)

