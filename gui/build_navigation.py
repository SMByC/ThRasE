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
from pathlib import Path

from qgis.core import QgsMapLayerProxyModel, QgsUnitTypes, Qgis
from qgis.gui import QgsMapToolPan
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QMessageBox, QColorDialog
from qgis.PyQt.QtCore import pyqtSlot

from ThRasE.utils.qgis_utils import load_and_select_filepath_in

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'build_navigation.ui'))


class BuildNavigation(QDialog, FORM_CLASS):

    def __init__(self, parent=None, layer_to_edit=None):
        QDialog.__init__(self, parent)
        self.setupUi(self)
        self.layer_to_edit = layer_to_edit

        self.setup_gui()

        # init render widget
        self.render_widget.canvas.setDestinationCrs(self.layer_to_edit.qgs_layer.crs())
        self.render_widget.canvas.setLayers([self.layer_to_edit.qgs_layer])
        self.render_widget.canvas.setExtent(self.layer_to_edit.extent())
        self.render_widget.refresh()

        self.map_tool_pan = QgsMapToolPan(self.render_widget.canvas)
        self.render_widget.canvas.setMapTool(self.map_tool_pan, clean=True)

    def setup_gui(self):
        self.NavTiles_widgetFile.setHidden(True)
        self.NavTiles_widgetAOI.setHidden(True)
        self.QCBox_BuildNavType.currentIndexChanged[str].connect(self.set_navigation_type_tool)
        # set properties to QgsMapLayerComboBox
        self.QCBox_VectorFile.setCurrentIndex(-1)
        self.QCBox_VectorFile.setFilters(QgsMapLayerProxyModel.VectorLayer)
        # handle connect layer selection with render canvas
        self.QCBox_VectorFile.currentIndexChanged.connect(
            lambda: self.render_over_thematic(self.QCBox_VectorFile.currentLayer()))
        # call to browse the render file
        self.QPBtn_BrowseVectorFile.clicked.connect(lambda: self.fileDialog_browse(
            self.QCBox_VectorFile,
            dialog_title=self.tr("Select the vector file"),
            dialog_types=self.tr("Vector files (*.gpkg *.shp);;All files (*.*)"),
            layer_type="vector"))
        self.QPBtn_BuildNavigation.clicked.connect(self.call_to_build_navigation)
        self.TilesColor.clicked.connect(self.change_tiles_color)

        # #### setup units in tile size
        # set/update the units in tileSize item
        layer_unit = self.layer_to_edit.qgs_layer.crs().mapUnits()
        str_unit = QgsUnitTypes.toString(layer_unit)
        abbr_unit = QgsUnitTypes.toAbbreviatedString(layer_unit)
        # Set the properties of the QdoubleSpinBox based on the QgsUnitTypes of the thematic layer
        # https://qgis.org/api/classQgsUnitTypes.html
        self.tileSize.setSuffix(" {}".format(abbr_unit))
        self.tileSize.setToolTip("The height/width of the tile to build the navigation, in {}\n"
                                 "(units based on the current thematic layer to edit)".format(str_unit))
        self.tileSize.setRange(0, 360 if layer_unit == QgsUnitTypes.DistanceDegrees else 10e10)
        self.tileSize.setDecimals(
            4 if layer_unit in [QgsUnitTypes.DistanceKilometers, QgsUnitTypes.DistanceNauticalMiles,
                                     QgsUnitTypes.DistanceMiles, QgsUnitTypes.DistanceDegrees] else 1)
        self.tileSize.setSingleStep(
            0.0001 if layer_unit in [QgsUnitTypes.DistanceKilometers, QgsUnitTypes.DistanceNauticalMiles,
                                          QgsUnitTypes.DistanceMiles, QgsUnitTypes.DistanceDegrees] else 1)
        default_tile_size = {QgsUnitTypes.DistanceMeters: 15000, QgsUnitTypes.DistanceKilometers: 15,
                             QgsUnitTypes.DistanceFeet: 49125, QgsUnitTypes.DistanceNauticalMiles: 8.125,
                             QgsUnitTypes.DistanceYards: 16500, QgsUnitTypes.DistanceMiles: 9.375,
                             QgsUnitTypes.DistanceDegrees: 0.1375, QgsUnitTypes.DistanceCentimeters: 1500000,
                             QgsUnitTypes.DistanceMillimeters: 15000000}
        self.tileSize.setValue(default_tile_size[layer_unit])

    @pyqtSlot()
    def exec_(self):
        if self.layer_to_edit.navigation.is_valid:
            [tile.create(self.render_widget.canvas) for tile in self.layer_to_edit.navigation.tiles]
        super().exec_()

    @pyqtSlot()
    def fileDialog_browse(self, combo_box, dialog_title, dialog_types, layer_type):
        file_path, _ = QFileDialog.getOpenFileName(self, dialog_title, "", dialog_types)
        if file_path != '' and os.path.isfile(file_path):
            # load to qgis and update combobox list
            load_and_select_filepath_in(combo_box, file_path, layer_type)

            self.render_over_thematic(combo_box.currentLayer())

    @pyqtSlot()
    def render_over_thematic(self, layer):
        if not layer:
            return
        pass

    @pyqtSlot()
    def change_tiles_color(self):
        color = QColorDialog.getColor(self.layer_to_edit.navigation.tiles_color, self)
        if color.isValid():
            self.layer_to_edit.navigation.tiles_color = color
            self.TilesColor.setStyleSheet("QToolButton{{background-color:{};}}".format(color.name()))
            # repaint
            if self.layer_to_edit.navigation.is_valid:
                self.call_to_build_navigation()

    def set_navigation_type_tool(self, nav_type):
        if nav_type == "by tiles throughout the thematic file":
            self.NavTiles_widgetFile.setHidden(True)
            self.NavTiles_widgetAOI.setHidden(True)
        if nav_type == "by tiles throughout the AOI":
            self.NavTiles_widgetFile.setHidden(True)
            self.NavTiles_widgetAOI.setVisible(True)
        if nav_type == "by tiles throughout a shapefile":
            self.NavTiles_widgetFile.setVisible(True)
            self.NavTiles_widgetAOI.setHidden(True)
        if nav_type == "by tiles throughout points":
            self.NavTiles_widgetFile.setVisible(True)
            self.NavTiles_widgetAOI.setHidden(True)
        if nav_type == "by tiles throughout centroid of polygons":
            self.NavTiles_widgetFile.setVisible(True)
            self.NavTiles_widgetAOI.setHidden(True)

    def call_to_build_navigation(self):
        # first prompt if the user do some progress in tile navigation
        if self.layer_to_edit.navigation.current_tile is not None and self.layer_to_edit.navigation.current_tile.idx != 1:
            quit_msg = "If you build another navigation you will lose the current progress " \
                       "(the current tile position).\n\nDo you want to continue?"
            reply = QMessageBox.question(None, 'Building the tile navigation',
                                         quit_msg, QMessageBox.Yes, QMessageBox.No)
            if reply == QMessageBox.No:
                return

        tile_size = self.tileSize.value()
        nav_mode = "horizontal" if self.nav_horizontal_mode.isChecked() else "vertical"
        nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode)
        if nav_status:  # navigation is valid
            self.layer_to_edit.navigation.is_valid = True
        else:  # navigation is not valid
            self.layer_to_edit.navigation.is_valid = False
            self.MsgBar.pushMessage("Navigation is not valid, check the settings", level=Qgis.Critical)

