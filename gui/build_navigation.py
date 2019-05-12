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

from qgis.core import QgsMapLayerProxyModel, QgsUnitTypes, Qgis, QgsWkbTypes, QgsFeature
from qgis.gui import QgsMapToolPan, QgsRubberBand, QgsMapTool
from qgis.PyQt import uic
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QMessageBox, QColorDialog
from qgis.PyQt.QtCore import pyqtSlot, Qt

from ThRasE.utils.qgis_utils import load_and_select_filepath_in

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'build_navigation.ui'))


class BuildNavigation(QDialog, FORM_CLASS):

    def __init__(self, parent=None, layer_to_edit=None):
        QDialog.__init__(self, parent)
        self.setupUi(self)
        self.layer_to_edit = layer_to_edit
        self.aoi_drawn = []

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
            file_filters=self.tr("Vector files (*.gpkg *.shp);;All files (*.*)")))
        # buttons connections
        self.QPBtn_BuildNavigation.clicked.connect(self.call_to_build_navigation)
        self.TilesColor.clicked.connect(self.change_tiles_color)
        self.CleanNavigation.clicked.connect(self.clean_navigation)
        self.AOI_Picker.clicked.connect(self.activate_deactivate_AOI_picker)
        self.DeleteAllAOI.clicked.connect(self.clean_all_aoi_drawn)

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
    def fileDialog_browse(self, combo_box, dialog_title, file_filters):
        file_path, _ = QFileDialog.getOpenFileName(self, dialog_title, "", file_filters)
        if file_path != '' and os.path.isfile(file_path):
            # load to qgis and update combobox list
            load_and_select_filepath_in(combo_box, file_path)

            self.render_over_thematic(combo_box.currentLayer())

    @pyqtSlot()
    def render_over_thematic(self, layer):
        if not layer:
            self.render_widget.canvas.setLayers([self.layer_to_edit.qgs_layer])
            self.render_widget.refresh()
            return

        self.render_widget.canvas.setLayers([layer, self.layer_to_edit.qgs_layer])
        self.render_widget.refresh()

    @pyqtSlot()
    def change_tiles_color(self, color=None):
        if not color:
            color = QColorDialog.getColor(self.layer_to_edit.navigation.tiles_color, self)
        if color.isValid():
            self.layer_to_edit.navigation.tiles_color = color
            self.TilesColor.setStyleSheet("QToolButton{{background-color:{};}}".format(color.name()))

    def set_navigation_type_tool(self, nav_type):
        # first unselect the vector file
        self.QCBox_VectorFile.setCurrentIndex(-1)
        # clear draw
        self.clean_all_aoi_drawn()
        # by nav type
        if nav_type == "by tiles throughout the thematic file":
            self.NavTiles_widgetFile.setHidden(True)
            self.NavTiles_widgetAOI.setHidden(True)
        if nav_type == "by tiles throughout the AOI":
            self.NavTiles_widgetFile.setHidden(True)
            self.NavTiles_widgetAOI.setVisible(True)
        if nav_type == "by tiles throughout polygons":
            self.NavTiles_widgetFile.setVisible(True)
            self.NavTiles_widgetAOI.setHidden(True)
            self.QCBox_VectorFile.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        if nav_type == "by tiles throughout points":
            self.NavTiles_widgetFile.setVisible(True)
            self.NavTiles_widgetAOI.setHidden(True)
            self.QCBox_VectorFile.setFilters(QgsMapLayerProxyModel.PointLayer)
        if nav_type == "by tiles throughout centroid of polygons":
            self.NavTiles_widgetFile.setVisible(True)
            self.NavTiles_widgetAOI.setHidden(True)
            self.QCBox_VectorFile.setFilters(QgsMapLayerProxyModel.PolygonLayer)

    @pyqtSlot()
    def activate_deactivate_AOI_picker(self):
        if isinstance(self.render_widget.canvas.mapTool(), AOIPickerTool):
            # disable edit and return to normal map tool
            self.render_widget.canvas.mapTool().finish()
        else:
            # enable draw
            self.render_widget.canvas.setMapTool(AOIPickerTool(self), clean=True)

    def clean_all_aoi_drawn(self):
        # clean/reset all rubber bands
        for rubber_band in self.aoi_drawn:
            rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.aoi_drawn = []
        if isinstance(self.render_widget.canvas.mapTool(), AOIPickerTool):
            self.render_widget.canvas.mapTool().finish()
        self.DeleteAllAOI.setEnabled(False)

    def call_to_build_navigation(self):
        # first prompt if the user do some progress in tile navigation
        if self.layer_to_edit.navigation.current_tile is not None and self.layer_to_edit.navigation.current_tile.idx != 1:
            quit_msg = "If you build another navigation you will lose the progress " \
                       "(the current tile position).\n\nDo you want to continue?"
            reply = QMessageBox.question(None, 'Building the tile navigation',
                                         quit_msg, QMessageBox.Yes, QMessageBox.No)
            if reply == QMessageBox.No:
                return

        tile_size = self.tileSize.value()
        nav_mode = "horizontal" if self.nav_horizontal_mode.isChecked() else "vertical"

        # call build navigation of the respective navigation type

        if self.QCBox_BuildNavType.currentText() == "by tiles throughout the thematic file":
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode)

        if self.QCBox_BuildNavType.currentText() == "by tiles throughout the AOI":
            if not self.aoi_drawn:
                self.MsgBar.pushMessage("Navigation was not built: there aren't polygons drawn", level=Qgis.Warning)
                return
            aois = [aoi.asGeometry() for aoi in self.aoi_drawn]
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode, polygons=aois)

        if self.QCBox_BuildNavType.currentText() == "by tiles throughout polygons":
            vector_layer = self.QCBox_VectorFile.currentLayer()
            if not vector_layer:
                self.MsgBar.pushMessage("First select a valid vector file of polygons", level=Qgis.Warning)
                return
            polygons = [feature.geometry() for feature in vector_layer.getFeatures()]
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode, polygons=polygons)

        if self.QCBox_BuildNavType.currentText() == "by tiles throughout points":
            vector_layer = self.QCBox_VectorFile.currentLayer()
            if not vector_layer:
                self.MsgBar.pushMessage("First select a valid vector file of points", level=Qgis.Warning)
                return
            points = [feature.geometry().asPoint() for feature in vector_layer.getFeatures()]
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode, points=points)

        if self.QCBox_BuildNavType.currentText() == "by tiles throughout centroid of polygons":
            vector_layer = self.QCBox_VectorFile.currentLayer()
            if not vector_layer:
                self.MsgBar.pushMessage("First select a valid vector file of polygons", level=Qgis.Warning)
                return
            polygons = [feature.geometry() for feature in vector_layer.getFeatures()]
            points = [polygon.centroid().asPoint() for polygon in polygons]
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode, points=points)

        if nav_status:  # navigation is valid
            self.layer_to_edit.navigation.is_valid = True
            self.CleanNavigation.setEnabled(True)
        else:  # navigation is not valid
            self.layer_to_edit.navigation.is_valid = False
            self.CleanNavigation.setEnabled(False)
            self.MsgBar.pushMessage("Navigation is not valid, check the settings", level=Qgis.Critical)

    def clean_navigation(self):
        # first prompt if the user do some progress in tile navigation
        if self.layer_to_edit.navigation.current_tile is not None and self.layer_to_edit.navigation.current_tile.idx != 1:
            quit_msg = "Clean the current navigation you will lose the progress " \
                       "(the current tile position).\n\nDo you want to continue?"
            reply = QMessageBox.question(None, 'Building the tile navigation',
                                         quit_msg, QMessageBox.Yes, QMessageBox.No)
            if reply == QMessageBox.No:
                return

        self.layer_to_edit.navigation.delete()
        self.CleanNavigation.setEnabled(False)


class AOIPickerTool(QgsMapTool):
    def __init__(self, build_navigation):
        QgsMapTool.__init__(self, build_navigation.render_widget.canvas)
        self.build_navigation = build_navigation
        # status rec icon and focus
        self.build_navigation.render_widget.canvas.setFocus()

        self.start_new_polygon()

    def start_new_polygon(self):
        # set rubber band style
        color = QColor("red")
        color.setAlpha(40)
        # create the main polygon rubber band
        self.rubber_band = QgsRubberBand(self.build_navigation.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(color)
        self.rubber_band.setWidth(3)
        # create the mouse/tmp polygon rubber band, this is main rubber band + current mouse position
        self.aux_rubber_band = QgsRubberBand(self.build_navigation.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
        self.aux_rubber_band.setColor(color)
        self.aux_rubber_band.setWidth(3)

    def finish(self):
        if self.rubber_band:
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        if self.aux_rubber_band:
            self.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.rubber_band = None
        self.aux_rubber_band = None
        self.build_navigation.AOI_Picker.setChecked(False)
        # restart point tool
        self.clean()
        self.build_navigation.render_widget.canvas.unsetMapTool(self)
        self.build_navigation.render_widget.canvas.setMapTool(self.build_navigation.map_tool_pan)

    def define_polygon(self):
        # clean the aux rubber band
        self.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.aux_rubber_band = None
        # adjust the color
        color = QColor("red")
        color.setAlpha(80)
        self.rubber_band.setColor(color)
        self.rubber_band.setWidth(3)
        # save
        new_feature = QgsFeature()
        new_feature.setGeometry(self.rubber_band.asGeometry())
        self.build_navigation.aoi_drawn.append(self.rubber_band)
        #
        self.build_navigation.DeleteAllAOI.setEnabled(True)

        self.start_new_polygon()

    def canvasMoveEvent(self, event):
        # draw the auxiliary rubber band
        if self.aux_rubber_band is None:
            return
        if self.aux_rubber_band and self.aux_rubber_band.numberOfVertices():
            x = event.pos().x()
            y = event.pos().y()
            point = self.build_navigation.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
            self.aux_rubber_band.removeLastPoint()
            self.aux_rubber_band.addPoint(point)

    def canvasPressEvent(self, event):
        if self.rubber_band is None:
            self.finish()
            return
        # new point on polygon
        if event.button() == Qt.LeftButton:
            x = event.pos().x()
            y = event.pos().y()
            point = self.build_navigation.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
            self.rubber_band.addPoint(point)
            self.aux_rubber_band.addPoint(point)
        # edit
        if event.button() == Qt.RightButton:
            if self.rubber_band and self.rubber_band.numberOfVertices():
                if self.rubber_band.numberOfVertices() < 3:
                    return
                # save polygon and edit
                self.define_polygon()

    def keyPressEvent(self, event):
        # edit
        if event.key() == Qt.Key_Enter or event.key() == Qt.Key_Return:
            if self.rubber_band and self.rubber_band.numberOfVertices():
                if self.rubber_band.numberOfVertices() < 3:
                    return
                # save polygon
                self.define_polygon()
        # delete last point
        if event.key() == Qt.Key_Backspace or event.key() == Qt.Key_Delete:
            self.rubber_band.removeLastPoint()
            self.aux_rubber_band.removeLastPoint()
        # delete and finish
        if event.key() == Qt.Key_Escape:
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.finish()
