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
from pathlib import Path

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import pyqtSignal, Qt, pyqtSlot
from qgis.PyQt.QtWidgets import QMessageBox, QGridLayout, QFileDialog, QTableWidgetItem
from qgis.core import Qgis, QgsMapLayer
from qgis.PyQt.QtGui import QColor, QFont

from ThRasE.core.edition import LayerToEdit
from ThRasE.gui.about_dialog import AboutDialog
from ThRasE.gui.build_navigation import BuildNavigation
from ThRasE.gui.view_widget import ViewWidget
from ThRasE.utils.qgis_utils import load_and_select_filepath_in, valid_file_selected_in

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
    instance = None
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
        # save instance
        ThRasEDialog.instance = self

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
        self.build_navigation_dialog = BuildNavigation()
        self.QPBtn_BuildNavigation.clicked.connect(self.open_build_navigation_dialog)

        # ######### build the view render widgets windows ######### #
        grid_rows = 1
        grid_columns = 2
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
        # handle connect layer selection
        self.QCBox_LayerToEdit.layerChanged.connect(self.select_layer_to_edit)
        self.QCBox_band_LayerToEdit.currentIndexChanged.connect(self.setup_layer_to_edit)
        # call to browse the render file
        self.QPBtn_browseLayerToEdit.clicked.connect(self.browse_dialog_layer_to_edit)
        # update recode pixel table
        self.recodePixelTable.itemChanged.connect(self.update_recode_pixel_table)

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
    def open_build_navigation_dialog(self):
        if self.build_navigation_dialog.exec_():
            # Build Navigation button
            pass
        else:
            # cancel button
            pass

    @pyqtSlot(QgsMapLayer)
    def select_layer_to_edit(self, layer_selected):
        # first clear table
        self.recodePixelTable.setRowCount(0)
        self.recodePixelTable.setColumnCount(0)
        # first check
        if layer_selected is None or not valid_file_selected_in(self.QCBox_LayerToEdit, "thematic layer to edit"):
            self.NavigationBlockWidget.setDisabled(True)
            self.QCBox_LayerToEdit.setCurrentIndex(-1)
            with block_signals_to(self.QCBox_band_LayerToEdit):
                self.QCBox_band_LayerToEdit.clear()
            LayerToEdit.current = None
            return
        # check if thematic layer to edit has data type as integer or byte
        if layer_selected.dataProvider().dataType(1) not in [1, 2, 3, 4, 5]:
            self.MsgBar.pushMessage("The thematic raster layer to edit must be byte or integer as data type",
                                    level=Qgis.Warning)
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
            layer_to_edit = LayerToEdit(layer, band)

        LayerToEdit.current = layer_to_edit
        self.set_recode_pixel_table()

        # enable some components
        self.NavigationBlockWidget.setEnabled(True)

    def update_recode_pixel_table(self):
        layer_to_edit = LayerToEdit.current
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
            except:
                pass
            # assign the on state
            on = self.recodePixelTable.item(row_idx, 3)
            if on.checkState() == 2:
                pixel["on"] = True
            if on.checkState() == 0:
                pixel["on"] = False

        self.set_recode_pixel_table()

    def set_recode_pixel_table(self):
        layer_to_edit = LayerToEdit.current

        if not layer_to_edit or layer_to_edit.pixels is None:
            # clear table
            self.recodePixelTable.setRowCount(0)
            self.recodePixelTable.setColumnCount(0)
            return

        with block_signals_to(self.recodePixelTable):
            header = ["", "old value", "new value", "on"]
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
                        item_table.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                        item_table.setBackground(QColor(pixel["color"]["R"], pixel["color"]["G"],
                                                        pixel["color"]["B"], pixel["color"]["A"]))
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)
                if header == "old value":
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem(str(pixel["value"]))
                        item_table.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        if not pixel["on"]:
                            item_table.setForeground(QColor("lightGrey"))
                            item_table.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                        if pixel["new_value"] and pixel["new_value"] != pixel["value"]:
                            font = QFont()
                            font.setBold(True)
                            item_table.setFont(font)
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)
                if header == "new value":
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem(str(pixel["new_value"]) if pixel["new_value"] else "")
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        if not pixel["on"]:
                            item_table.setForeground(QColor("lightGrey"))
                            item_table.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                        if pixel["new_value"] and pixel["new_value"] != pixel["value"]:
                            font = QFont()
                            font.setBold(True)
                            item_table.setFont(font)
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)
                if header == "on":
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem()
                        item_table.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        if pixel["on"]:
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

