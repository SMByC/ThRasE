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

from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog, QFileDialog
from qgis.PyQt.QtCore import pyqtSlot

from ThRasE.utils.qgis_utils import load_and_select_filepath_in

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'build_navigation.ui'))


class BuildNavigation(QDialog, FORM_CLASS):

    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.setupUi(self)
        self.thematic_to_edit = None

    def setup_gui(self, thematic_to_edit):
        self.thematic_to_edit = thematic_to_edit
        self.NavTiles_widgetFile.setHidden(True)
        self.NavTiles_widgetAOI.setHidden(True)
        self.QCBox_BuildNavType.currentIndexChanged[str].connect(self.set_navigation_type_tool)
        # set properties to QgsMapLayerComboBox
        self.QCBox_BuildNavFile.setCurrentIndex(-1)
        # handle connect layer selection with render canvas
        self.QCBox_BuildNavFile.currentIndexChanged.connect(
            lambda: self.render_over_thematic(self.QCBox_BuildNavFile.currentLayer()))
        # call to browse the render file
        self.QPBtn_BrowseBuildNavFile.clicked.connect(lambda: self.fileDialog_browse(
            self.QCBox_BuildNavFile,
            dialog_title=self.tr("Select the NN file"),
            dialog_types=self.tr("Raster or vector files (*.tif *.img *.gpkg *.shp);;All files (*.*)"),
            layer_type="any"))

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
