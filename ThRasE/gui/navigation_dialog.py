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

from qgis.core import QgsMapLayerProxyModel, QgsUnitTypes, Qgis, QgsWkbTypes, QgsFeature, QgsCoordinateReferenceSystem, \
    QgsCoordinateTransform, QgsProject, QgsRectangle
from qgis.gui import QgsMapToolPan, QgsRubberBand, QgsMapTool
from qgis.PyQt import uic
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QMessageBox, QColorDialog
from qgis.PyQt.QtCore import pyqtSlot, Qt, QTimer

from ThRasE.utils.qgis_utils import load_and_select_filepath_in
from ThRasE.utils.system_utils import block_signals_to

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'navigation_dialog.ui'))


class NavigationDialog(QDialog, FORM_CLASS):

    def __init__(self, parent=None, layer_to_edit=None):
        QDialog.__init__(self, parent)
        self.setupUi(self)
        self.layer_to_edit = layer_to_edit
        self.aoi_drawn = []
        self.highlight_tile = None

        self.setup_gui()

        # init render widget
        self.render_widget.canvas.setDestinationCrs(self.layer_to_edit.qgs_layer.crs())
        self.render_widget.canvas.setLayers([self.layer_to_edit.qgs_layer])
        self.render_widget.canvas.setExtent(self.layer_to_edit.extent())
        self.render_widget.refresh()

        self.map_tool_pan = QgsMapToolPan(self.render_widget.canvas)
        self.render_widget.canvas.setMapTool(self.map_tool_pan, clean=True)

        # flags
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinimizeButtonHint)

    def setup_gui(self):
        self.NavTiles_widgetFile.setHidden(True)
        self.NavTiles_widgetAOI.setHidden(True)
        self.SliderNavigationBlock.setEnabled(False)
        self.QCBox_BuildNavType.currentIndexChanged[str].connect(self.set_navigation_type_tool)
        # set properties to QgsMapLayerComboBox
        self.QCBox_VectorFile.setCurrentIndex(-1)
        self.QCBox_VectorFile.setFilters(QgsMapLayerProxyModel.VectorLayer)
        # handle connect layer selection with render canvas
        self.QCBox_VectorFile.currentIndexChanged.connect(
            lambda: self.render_over_thematic(self.QCBox_VectorFile.currentLayer()))
        # call to browse the render file
        self.QPBtn_BrowseVectorFile.clicked.connect(lambda: self.browser_dialog_to_load_file(
            self.QCBox_VectorFile,
            dialog_title=self.tr("Select the vector file"),
            file_filters=self.tr("Vector files (*.gpkg *.shp);;All files (*.*)")))
        # buttons connections
        self.QPBtn_BuildNavigationTools.clicked.connect(self.build_tools)
        self.QPBtn_BuildNavigation.clicked.connect(self.call_to_build_navigation)
        self.TilesColor.clicked.connect(self.change_tiles_color)
        self.DeleteNavigation.clicked.connect(self.delete_navigation)
        self.AOI_Picker.clicked.connect(self.activate_deactivate_AOI_picker)
        self.DeleteAllAOI.clicked.connect(self.clear_all_aoi_drawn)
        self.ZoomToTiles.clicked.connect(self.zoom_to_tiles)
        # slider and spinbox connection
        self.SliderNavigation.valueChanged.connect(self.highlight)
        self.SliderNavigation.sliderReleased.connect(lambda: self.change_tile_from_slider(self.SliderNavigation.value()))
        self.currentTile.valueChanged.connect(self.change_tile_from_spinbox)
        # #### setup units in tile size
        # set/update the units in tileSize item
        layer_unit = self.layer_to_edit.qgs_layer.crs().mapUnits()
        if layer_unit == QgsUnitTypes.DistanceUnknownUnit:
            layer_unit = QgsUnitTypes.DistanceMeters
            str_unit = QgsUnitTypes.toString(layer_unit) + \
                       "\nWARNING: The layer does not have a valid map unit, meters will be used as the base unit."
        else:
            str_unit = QgsUnitTypes.toString(layer_unit)
        abbr_unit = QgsUnitTypes.toAbbreviatedString(layer_unit)
        # Set the properties of the QdoubleSpinBox based on the QgsUnitTypes of the thematic layer
        # https://qgis.org/api/classQgsUnitTypes.html
        self.tileSize.setSuffix(" {}".format(abbr_unit))
        self.tileSize.setToolTip("Defines the side length of the navigation tiles in {}.\n"
                                 "(units based on the current thematic layer to edit)\n"
                                 "(rebuild the navigation to make the changes)".format(str_unit))
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
    def build_tools(self):
        if self.QPBtn_BuildNavigationTools.isChecked():
            self.QPBtn_BuildNavigationTools.setArrowType(Qt.DownArrow)
            self.build_tools_line.setVisible(True)
            self.NavBuildTypeBlock.setVisible(True)
            self.NavBuildToolsBlock.setVisible(True)
        else:
            self.QPBtn_BuildNavigationTools.setArrowType(Qt.UpArrow)
            self.build_tools_line.setHidden(True)
            self.NavBuildTypeBlock.setHidden(True)
            self.NavBuildToolsBlock.setHidden(True)

    @pyqtSlot()
    def browser_dialog_to_load_file(self, combo_box, dialog_title, file_filters):
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
            # update navigation color state
            navigation = self.layer_to_edit.navigation
            navigation.tiles_color = color
            self.TilesColor.setStyleSheet("QToolButton{{background-color:{};}}".format(color.name()))

            # propagate new color to all tiles
            for tile in navigation.tiles:
                tile.tile_color = color

            # redraw in navigation dialog (all tiles and highlight)
            if navigation.is_valid and navigation.current_tile is not None:
                navigation.clear(rbs_in="nav_dialog")
                self.highlight(navigation.current_tile.idx)

            # redraw current tile in all active view widgets
            if navigation.is_valid and navigation.current_tile is not None:
                from ThRasE.thrase import ThRasE
                navigation.clear(rbs_in="main_dialog")
                navigation.current_tile.show()
                if not ThRasE.dialog.currentTileKeepVisible.isChecked():
                    QTimer.singleShot(1200, navigation.current_tile.hide)
                # refresh active view widgets
                from ThRasE.gui.main_dialog import ThRasEDialog
                for view_widget in ThRasEDialog.view_widgets:
                    if view_widget.is_active:
                        view_widget.render_widget.refresh()

    @pyqtSlot(str)
    def set_navigation_type_tool(self, nav_type):
        # first unselect the vector file
        self.QCBox_VectorFile.setCurrentIndex(-1)
        # clear draw
        self.clear_all_aoi_drawn()
        # by nav type
        if nav_type == "thematic file":
            self.NavTiles_widgetFile.setHidden(True)
            self.NavTiles_widgetAOI.setHidden(True)
        if nav_type == "AOIs":
            self.NavTiles_widgetFile.setHidden(True)
            self.NavTiles_widgetAOI.setVisible(True)
        if nav_type == "polygons":
            self.NavTiles_widgetFile.setVisible(True)
            self.NavTiles_widgetAOI.setHidden(True)
            self.QCBox_VectorFile.setFilters(QgsMapLayerProxyModel.PolygonLayer)
            self.QCBox_VectorFile.setToolTip("Select a polygon vector file to use as the\n"
                                             "base for building the navigation tiles")
        if nav_type == "points":
            self.NavTiles_widgetFile.setVisible(True)
            self.NavTiles_widgetAOI.setHidden(True)
            self.QCBox_VectorFile.setFilters(QgsMapLayerProxyModel.PointLayer)
            self.QCBox_VectorFile.setToolTip("Select a point vector file to use as the\n"
                                             "base for building the navigation tiles")
        if nav_type == "centroid of polygons":
            self.NavTiles_widgetFile.setVisible(True)
            self.NavTiles_widgetAOI.setHidden(True)
            self.QCBox_VectorFile.setFilters(QgsMapLayerProxyModel.PolygonLayer)
            self.QCBox_VectorFile.setToolTip("Select a polygon vector file to use as the\n"
                                             "base for building the navigation tiles\n"
                                             "using the centroid of each polygon")

    @pyqtSlot()
    def activate_deactivate_AOI_picker(self):
        if isinstance(self.render_widget.canvas.mapTool(), AOIPickerTool):
            # disable edit and return to normal map tool
            self.render_widget.canvas.mapTool().finish()
        else:
            # enable draw
            self.render_widget.canvas.setMapTool(AOIPickerTool(self), clean=True)

    @pyqtSlot()
    def clear_all_aoi_drawn(self):
        # clean/reset all rubber bands
        for rubber_band in self.aoi_drawn:
            rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.aoi_drawn = []
        if isinstance(self.render_widget.canvas.mapTool(), AOIPickerTool):
            self.render_widget.canvas.mapTool().finish()
        self.DeleteAllAOI.setEnabled(False)

    @pyqtSlot()
    def call_to_build_navigation(self):
        # first prompt if the user do some progress in navigation tile
        if self.layer_to_edit.navigation.current_tile is not None and self.layer_to_edit.navigation.current_tile.idx != 1:
            quit_msg = "If you build another navigation you will lose the progress " \
                       "(the current tile position).\n\nDo you want to continue?"
            reply = QMessageBox.question(None, 'Building the navigation tiles',
                                         quit_msg, QMessageBox.Yes, QMessageBox.No)
            if reply == QMessageBox.No:
                return

        tile_size = self.tileSize.value()
        nav_mode = "horizontal" if self.nav_horizontal_mode.isChecked() else "vertical"

        # call build navigation of the respective navigation type

        if self.QCBox_BuildNavType.currentText() == "thematic file":
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode)

        if self.QCBox_BuildNavType.currentText() == "AOIs":
            if not self.aoi_drawn:
                self.MsgBar.pushMessage("Navigation building failed: no polygons were drawn", level=Qgis.Warning, duration=10)
                return
            aois = [aoi.asGeometry() for aoi in self.aoi_drawn]
            # build navigation
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode, polygons=aois)

        if self.QCBox_BuildNavType.currentText() == "polygons":
            vector_layer = self.QCBox_VectorFile.currentLayer()
            if not vector_layer:
                self.MsgBar.pushMessage("First select a valid vector file of polygons", level=Qgis.Warning, duration=10)
                return
            geometries = [feature.geometry() for feature in vector_layer.getFeatures()]  # as polygons
            # convert all coordinates system of the input geometries to target crs of the thematic edit file
            crs_transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem(vector_layer.crs()),
                                                   QgsCoordinateReferenceSystem(self.layer_to_edit.qgs_layer.crs()),
                                                   QgsProject.instance())
            [geom.transform(crs_transform) for geom in geometries]
            # build navigation
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode, polygons=geometries)

        if self.QCBox_BuildNavType.currentText() == "points":
            vector_layer = self.QCBox_VectorFile.currentLayer()
            if not vector_layer:
                self.MsgBar.pushMessage("First select a valid vector file of points", level=Qgis.Warning, duration=10)
                return
            geometries = [feature.geometry() for feature in vector_layer.getFeatures()]  # as points
            # convert all coordinates system of the input geometries to target crs of the thematic edit file
            crs_transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem(vector_layer.crs()),
                                                   QgsCoordinateReferenceSystem(self.layer_to_edit.qgs_layer.crs()),
                                                   QgsProject.instance())
            [geom.transform(crs_transform) for geom in geometries]
            points = [geom.asPoint() for geom in geometries]
            # build navigation
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode, points=points)

        if self.QCBox_BuildNavType.currentText() == "centroid of polygons":
            vector_layer = self.QCBox_VectorFile.currentLayer()
            if not vector_layer:
                self.MsgBar.pushMessage("First select a valid vector file of polygons", level=Qgis.Warning, duration=10)
                return
            geometries = [feature.geometry() for feature in vector_layer.getFeatures()]  # as polygons
            # convert all coordinates system of the input geometries to target crs of the thematic edit file
            crs_transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem(vector_layer.crs()),
                                                   QgsCoordinateReferenceSystem(self.layer_to_edit.qgs_layer.crs()),
                                                   QgsProject.instance())
            [geom.transform(crs_transform) for geom in geometries]
            points = [geom.centroid().asPoint() for geom in geometries]
            # build navigation
            nav_status = self.layer_to_edit.navigation.build_navigation(tile_size, nav_mode, points=points)

        # finish build navigation
        from ThRasE.thrase import ThRasE
        if nav_status:  # navigation is valid
            self.layer_to_edit.navigation.is_valid = True
            self.DeleteNavigation.setEnabled(True)
            # set settings in the navigation dialog
            self.SliderNavigationBlock.setEnabled(True)
            self.totalTiles.setText("/{}".format(len(self.layer_to_edit.navigation.tiles)))
            self.SliderNavigation.setMaximum(len(self.layer_to_edit.navigation.tiles))
            self.currentTile.setValue(self.layer_to_edit.navigation.current_tile.idx)
            self.currentTile.setMaximum(len(self.layer_to_edit.navigation.tiles))
            # init and set the progress bar and navigation in main dialog
            ThRasE.dialog.NavigationBlockWidgetControls.setEnabled(True)
            ThRasE.dialog.QPBar_TilesNavigation.setMaximum(len(self.layer_to_edit.navigation.tiles))
            ThRasE.dialog.QPBar_TilesNavigation.setValue(self.layer_to_edit.navigation.current_tile.idx)
            ThRasE.dialog.previousTile.setEnabled(False)
            ThRasE.dialog.nextTile.setEnabled(True)
            self.highlight()
        else:  # navigation is not valid
            self.SliderNavigationBlock.setEnabled(False)
            self.layer_to_edit.navigation.is_valid = False
            self.DeleteNavigation.setEnabled(False)
            ThRasE.dialog.NavigationBlockWidgetControls.setEnabled(False)
            self.MsgBar.pushMessage("Navigation is not valid, check the settings", level=Qgis.Critical, duration=20)

    @pyqtSlot(int)
    def change_tile_from_slider(self, idx_tile):
        with block_signals_to(self.currentTile):
            self.currentTile.setValue(idx_tile)
        self.go_to_tile(idx_tile)

    @pyqtSlot(int)
    def change_tile_from_spinbox(self, idx_tile):
        with block_signals_to(self.SliderNavigation):
            self.SliderNavigation.setValue(idx_tile)
        self.go_to_tile(idx_tile)

    def go_to_tile(self, idx_tile):
        self.layer_to_edit.navigation.set_current_tile(idx_tile)

        # adjust navigation components in main dialog
        from ThRasE.thrase import ThRasE
        ThRasE.dialog.QPBar_TilesNavigation.setValue(idx_tile)
        if idx_tile == 1:  # first item
            ThRasE.dialog.previousTile.setEnabled(False)
        else:
            ThRasE.dialog.previousTile.setEnabled(True)
        if idx_tile == len(self.layer_to_edit.navigation.tiles):  # last item
            ThRasE.dialog.nextTile.setEnabled(False)
        else:
            ThRasE.dialog.nextTile.setEnabled(True)

        self.highlight()

    def highlight(self, idx_tile=None):
        if idx_tile:  # from slider

            with block_signals_to(self.currentTile):
                self.currentTile.setValue(idx_tile)

            # update the review tiles in the navigation dialog
            self.layer_to_edit.navigation.clear(rbs_in="nav_dialog")
            [tile.create(self.render_widget.canvas, rbs_in="nav_dialog", current_idx_tile=idx_tile) for tile in
             self.layer_to_edit.navigation.tiles]

        if idx_tile:
            tile = next((tile for tile in self.layer_to_edit.navigation.tiles if tile.idx == idx_tile), None)
        else:
            tile = self.layer_to_edit.navigation.current_tile

        # unhighlight the before tile (rubber band)
        if self.highlight_tile:
            self.highlight_tile.reset(QgsWkbTypes.PolygonGeometry)

        self.highlight_tile = tile.create(self.render_widget.canvas, line_width=6, rbs_in="highlight")

    @pyqtSlot()
    def delete_navigation(self):
        # first prompt if the user do some progress in navigation tiles
        if self.layer_to_edit.navigation.current_tile is not None and self.layer_to_edit.navigation.current_tile.idx != 1:
            quit_msg = "Clear the current navigation you will lose the progress " \
                       "(the current tile position).\n\nDo you want to continue?"
            reply = QMessageBox.question(None, 'Building the navigation tiles',
                                         quit_msg, QMessageBox.Yes, QMessageBox.No)
            if reply == QMessageBox.No:
                return

        self.layer_to_edit.navigation.delete()
        self.DeleteNavigation.setEnabled(False)

    @pyqtSlot()
    def zoom_to_tiles(self):
        if self.layer_to_edit.navigation.is_valid:
            # compute the wrapper extent of all tiles
            extent_tiles = QgsRectangle()
            [extent_tiles.combineExtentWith(tile.extent) for tile in self.layer_to_edit.navigation.tiles]
            extent_tiles = extent_tiles.buffered((extent_tiles.yMaximum() - extent_tiles.yMinimum()) * 0.02)
            self.render_widget.canvas.setExtent(extent_tiles)
            self.render_widget.refresh()
        else:
            self.render_widget.canvas.setExtent(self.layer_to_edit.extent())
            self.render_widget.refresh()


class AOIPickerTool(QgsMapTool):
    def __init__(self, navigation_dialog):
        QgsMapTool.__init__(self, navigation_dialog.render_widget.canvas)
        self.navigation_dialog = navigation_dialog
        # status rec icon and focus
        self.navigation_dialog.render_widget.canvas.setFocus()

        self.start_new_polygon()

    def start_new_polygon(self):
        # set rubber band style
        color = QColor("red")
        color.setAlpha(40)
        # create the main polygon rubber band
        self.rubber_band = QgsRubberBand(self.navigation_dialog.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(color)
        self.rubber_band.setWidth(3)
        # create the mouse/tmp polygon rubber band, this is main rubber band + current mouse position
        self.aux_rubber_band = QgsRubberBand(self.navigation_dialog.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
        self.aux_rubber_band.setColor(color)
        self.aux_rubber_band.setWidth(3)

    def finish(self):
        if self.rubber_band:
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        if self.aux_rubber_band:
            self.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.rubber_band = None
        self.aux_rubber_band = None
        self.navigation_dialog.AOI_Picker.setChecked(False)
        # restart point tool
        self.clean()
        self.navigation_dialog.render_widget.canvas.unsetMapTool(self)
        self.navigation_dialog.render_widget.canvas.setMapTool(self.navigation_dialog.map_tool_pan)

    def define_polygon(self):
        # clean the aux rubber band
        self.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.aux_rubber_band = None
        # adjust the color
        color = QColor("red")
        color.setAlpha(70)
        self.rubber_band.setColor(color)
        self.rubber_band.setWidth(3)
        # save
        new_feature = QgsFeature()
        new_feature.setGeometry(self.rubber_band.asGeometry())
        self.navigation_dialog.aoi_drawn.append(self.rubber_band)
        #
        self.navigation_dialog.DeleteAllAOI.setEnabled(True)

        self.start_new_polygon()

    def canvasMoveEvent(self, event):
        # draw the auxiliary rubber band
        if self.aux_rubber_band is None:
            return
        if self.aux_rubber_band and self.aux_rubber_band.numberOfVertices():
            x = event.pos().x()
            y = event.pos().y()
            point = self.navigation_dialog.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
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
            point = self.navigation_dialog.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
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
