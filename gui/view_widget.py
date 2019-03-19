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
from qgis.PyQt.QtWidgets import QWidget
from qgis.PyQt.QtCore import Qt, pyqtSlot, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsProject, QgsWkbTypes, QgsFeature, QgsRaster
from qgis.gui import QgsMapTool, QgsRubberBand

from ThRasE.core.edition import LayerToEdit, edit_layer
from ThRasE.utils.system_utils import block_signals_to, wait_process

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'view_widget.ui'))


class ViewWidget(QWidget, FORM_CLASS):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.id = None
        self.pc_id = None
        self.is_active = False
        self.active_layers = []  # for save the active layers instances
        self.setupUi(self)
        # init as unactivated render widget for new instances
        self.widget_ActiveLayers.setHidden(True)
        self.widget_EditionTools.setHidden(True)
        self.disable()

    def setup_view_widget(self):
        self.render_widget.parent_view = self
        # settings the ActiveLayers widget
        self.QPBtn_ConfActiveLayers.clicked.connect(self.active_layers_widget)
        self.QPBtn_EditionTools.clicked.connect(self.edition_tools_widget)

        # ### init the three active layers ###
        self.widget_ActiveLayer_1.setup_gui(1, self)
        self.widget_ActiveLayer_2.setup_gui(2, self)
        self.widget_ActiveLayer_3.setup_gui(3, self)
        # save active layers in render widget
        self.render_widget.active_layers = self.active_layers

        # ### init the edition tools ###
        # picker pixel tool edit
        self.widget_EditionTools.setEnabled(False)
        self.PixelsPicker.clicked.connect(self.use_pixels_picker_for_edit)

        # picker line tool edit
        self.lines_drawn = []
        self.LinesPicker.clicked.connect(self.use_lines_picker_for_edit)
        # clean actions
        self.CleanAllLines.clicked.connect(self.clean_all_lines_drawn)

        # picker polygon tool edit
        self.polygons_drawn = []
        self.PolygonsPicker.clicked.connect(self.use_polygons_picker_for_edit)
        # undo/redo
        self.UndoPixel.clicked.connect(lambda: self.go_to_history("undo", "pixel"))
        self.RedoPixel.clicked.connect(lambda: self.go_to_history("redo", "pixel"))
        self.UndoLine.clicked.connect(lambda: self.go_to_history("undo", "line"))
        self.RedoLine.clicked.connect(lambda: self.go_to_history("redo", "line"))
        self.UndoPolygon.clicked.connect(lambda: self.go_to_history("undo", "polygon"))
        self.RedoPolygon.clicked.connect(lambda: self.go_to_history("redo", "polygon"))
        # clean actions
        self.CleanAllPolygons.clicked.connect(self.clean_all_polygons_drawn)

    def clean(self):
        # clean this view widget and the layers loaded in PCs
        if self.pc_id is not None:
            for layer in self.render_widget.canvas.layers():
                QgsProject.instance().removeMapLayer(layer.id())
        self.disable()

    def update(self):
        valid_layers = [active_layer.layer for active_layer in self.active_layers if active_layer.is_active]
        if len(valid_layers) > 0:
            self.enable()
        else:
            self.disable()
        self.QPBtn_ConfActiveLayers.setText("{} active layers".format(len(valid_layers)))

    def enable(self):
        with block_signals_to(self.render_widget):
            # activate some parts of this view
            self.QLabel_ViewName.setEnabled(True)
            self.render_widget.setEnabled(True)
            self.render_widget.canvas.setCanvasColor(QColor(255, 255, 255))
            # set status for view widget
            self.is_active = True

    def disable(self):
        with block_signals_to(self.render_widget):
            self.render_widget.canvas.setLayers([])
            self.render_widget.canvas.clearCache()
            self.render_widget.canvas.refresh()
            # deactivate some parts of this view
            self.QLabel_ViewName.setDisabled(True)
            self.render_widget.setDisabled(True)
            self.render_widget.canvas.setCanvasColor(QColor(245, 245, 245))
            # set status for view widget
            self.is_active = False

    @pyqtSlot()
    def canvas_changed(self):
        if self.is_active:
            new_extent = self.render_widget.canvas.extent()
            # update canvas for all view activated except this view
            from ThRasE.gui.main_dialog import ThRasEDialog
            for view_widget in ThRasEDialog.view_widgets:
                # for all view widgets in main dialog
                if view_widget.is_active and view_widget != self:
                    view_widget.render_widget.update_canvas_to(new_extent)

    @wait_process
    @edit_layer
    def go_to_history(self, action, from_edit_tool):
        if from_edit_tool == "pixel":
            if action == "undo":
                point, value = LayerToEdit.current.history_pixels.undo()
            if action == "redo":
                point, value = LayerToEdit.current.history_pixels.redo()
            # make action
            LayerToEdit.current.edit_pixel(point, value)
            # update status of undo/redo buttons
            self.UndoPixel.setEnabled(LayerToEdit.current.history_pixels.can_be_undone())
            self.RedoPixel.setEnabled(LayerToEdit.current.history_pixels.can_be_redone())

        if from_edit_tool == "line":
            if action == "undo":
                line_feature, points_and_values = LayerToEdit.current.history_lines.undo()
                # delete the line
                rubber_band = next((rb for rb in self.lines_drawn if
                                    rb.asGeometry().equals(line_feature.geometry())), None)
                if rubber_band:
                    rubber_band.reset(QgsWkbTypes.LineGeometry)
                    self.lines_drawn.remove(rubber_band)
            if action == "redo":
                line_feature, points_and_values = LayerToEdit.current.history_lines.redo()
                # create, repaint and save the rubber band to redo
                rubber_band = QgsRubberBand(self.render_widget.canvas, QgsWkbTypes.LineGeometry)
                color = QColor("red")
                color.setAlpha(80)
                rubber_band.setColor(color)
                rubber_band.setWidth(3)
                rubber_band.addGeometry(line_feature.geometry())
                self.lines_drawn.append(rubber_band)
            # make action
            for point, value in points_and_values:
                LayerToEdit.current.edit_pixel(point, value)
            # update status of undo/redo/clean buttons
            self.UndoLine.setEnabled(LayerToEdit.current.history_lines.can_be_undone())
            self.RedoLine.setEnabled(LayerToEdit.current.history_lines.can_be_redone())
            self.CleanAllLines.setEnabled(len(self.lines_drawn) > 0)

        if from_edit_tool == "polygon":
            if action == "undo":
                polygon_feature, points_and_values = LayerToEdit.current.history_polygons.undo()
                # delete the rubber band
                rubber_band = next((rb for rb in self.polygons_drawn if
                                    rb.asGeometry().equals(polygon_feature.geometry())), None)
                if rubber_band:
                    rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                    self.polygons_drawn.remove(rubber_band)
            if action == "redo":
                polygon_feature, points_and_values = LayerToEdit.current.history_polygons.redo()
                # create, repaint and save the rubber band to redo
                rubber_band = QgsRubberBand(self.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
                color = QColor("red")
                color.setAlpha(50)
                rubber_band.setColor(color)
                rubber_band.setWidth(2)
                rubber_band.addGeometry(polygon_feature.geometry())
                self.polygons_drawn.append(rubber_band)
            # make action
            for point, value in points_and_values:
                LayerToEdit.current.edit_pixel(point, value)
            # update status of undo/redo buttons
            self.UndoPolygon.setEnabled(LayerToEdit.current.history_polygons.can_be_undone())
            self.RedoPolygon.setEnabled(LayerToEdit.current.history_polygons.can_be_redone())
            self.CleanAllPolygons.setEnabled(len(self.polygons_drawn) > 0)
        # update changes done in the layer and view
        LayerToEdit.current.qgs_layer.reload()
        LayerToEdit.current.qgs_layer.triggerRepaint()
        self.render_widget.canvas.clearCache()
        self.render_widget.canvas.refresh()

    @pyqtSlot()
    def active_layers_widget(self):
        # open/close all active layers widgets
        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            # open to close
            if view_widget.widget_ActiveLayers.isVisible():
                view_widget.widget_ActiveLayers.setHidden(True)
                view_widget.QPBtn_ConfActiveLayers.setArrowType(Qt.DownArrow)
            # close to open
            else:
                view_widget.widget_ActiveLayers.setVisible(True)
                view_widget.QPBtn_ConfActiveLayers.setArrowType(Qt.UpArrow)

        # refresh all extents based on the first active view
        actives_view_widget = [view_widget for view_widget in ThRasEDialog.view_widgets if view_widget.is_active]
        if actives_view_widget:
            QTimer.singleShot(10, lambda: actives_view_widget[0].canvas_changed())

    @pyqtSlot()
    def edition_tools_widget(self):
        # open/close all active layers widgets
        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            # open to close
            if view_widget.widget_EditionTools.isVisible():
                view_widget.widget_EditionTools.setHidden(True)
                view_widget.QPBtn_EditionTools.setArrowType(Qt.DownArrow)
            # close to open
            else:
                view_widget.widget_EditionTools.setVisible(True)
                view_widget.QPBtn_EditionTools.setArrowType(Qt.UpArrow)

        # refresh all extents based on the first active view
        actives_view_widget = [view_widget for view_widget in ThRasEDialog.view_widgets if view_widget.is_active]
        if actives_view_widget:
            QTimer.singleShot(10, lambda: actives_view_widget[0].canvas_changed())

    @pyqtSlot()
    def use_pixels_picker_for_edit(self):
        if isinstance(self.render_widget.canvas.mapTool(), PickerPixelTool):
            # disable edit and return to normal map tool
            self.render_widget.canvas.mapTool().finish()
        else:
            # finish the other picker activation
            if isinstance(self.render_widget.canvas.mapTool(), (PickerLineTool, PickerPolygonTool)):
                self.render_widget.canvas.mapTool().finish()
            # enable edit
            self.render_widget.canvas.setMapTool(PickerPixelTool(self), clean=True)

    @pyqtSlot()
    def use_lines_picker_for_edit(self):
        if isinstance(self.render_widget.canvas.mapTool(), PickerLineTool):
            # disable edit and return to normal map tool
            self.render_widget.canvas.mapTool().finish()
        else:
            # finish the other picker activation
            if isinstance(self.render_widget.canvas.mapTool(), (PickerPixelTool, PickerPolygonTool)):
                self.render_widget.canvas.mapTool().finish()
            # enable edit
            self.render_widget.canvas.setMapTool(PickerLineTool(self), clean=True)

    @pyqtSlot()
    def use_polygons_picker_for_edit(self):
        if isinstance(self.render_widget.canvas.mapTool(), PickerPolygonTool):
            # disable edit and return to normal map tool
            self.render_widget.canvas.mapTool().finish()
        else:
            # finish the other picker activation
            if isinstance(self.render_widget.canvas.mapTool(), (PickerPixelTool, PickerLineTool)):
                self.render_widget.canvas.mapTool().finish()
            # enable edit
            self.render_widget.canvas.setMapTool(PickerPolygonTool(self), clean=True)

    def clean_all_lines_drawn(self):
        # clean/reset all rubber bands
        for rubber_band in self.lines_drawn:
            rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.lines_drawn = []
        self.CleanAllLines.setEnabled(False)

    def clean_all_polygons_drawn(self):
        # clean/reset all rubber bands
        for rubber_band in self.polygons_drawn:
            rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.polygons_drawn = []
        self.CleanAllPolygons.setEnabled(False)


class PickerPixelTool(QgsMapTool):
    def __init__(self, view_widget):
        QgsMapTool.__init__(self, view_widget.render_widget.canvas)
        self.view_widget = view_widget
        # status rec icon and focus
        self.view_widget.status_PixelsPicker.setEnabled(True)
        self.view_widget.status_PixelsPicker.clicked.connect(self.finish)
        self.view_widget.render_widget.canvas.setFocus()

    def finish(self):
        self.view_widget.status_PixelsPicker.setDisabled(True)
        # restart point tool
        self.clean()
        self.view_widget.render_widget.canvas.unsetMapTool(self)
        self.view_widget.render_widget.canvas.setMapTool(self.view_widget.render_widget.default_point_tool)

    def edit(self, event):
        x = event.pos().x()
        y = event.pos().y()
        point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
        status = LayerToEdit.current.edit_from_pixel_picker(point)
        if status:
            self.view_widget.render_widget.canvas.clearCache()
            self.view_widget.render_widget.canvas.refresh()
            # update status of undo/redo buttons
            self.view_widget.UndoPixel.setEnabled(LayerToEdit.current.history_pixels.can_be_undone())
            self.view_widget.RedoPixel.setEnabled(LayerToEdit.current.history_pixels.can_be_redone())

    def canvasMoveEvent(self, event):
        # highlight the current pixel value from mouse picker
        if self.view_widget.mousePixelValue2Table.isChecked():
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
            pixel_value_to_select = LayerToEdit.current.data_provider.identify(point, QgsRaster.IdentifyFormatValue).results()[1]
            if pixel_value_to_select is not None:
                LayerToEdit.current.select_value_in_recode_pixel_table(pixel_value_to_select)

    def canvasPressEvent(self, event):
        # edit the pixel over pointer mouse on left-click
        if event.button() == Qt.LeftButton:
            self.edit(event)

    def wheelEvent(self, event):
        QgsMapTool.wheelEvent(self, event)
        QTimer.singleShot(10, self.view_widget.canvas_changed)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.finish()

    def keyReleaseEvent(self, event):
        if event.key() in [Qt.Key_Up, Qt.Key_Down, Qt.Key_Right, Qt.Key_Left, Qt.Key_PageUp, Qt.Key_PageDown]:
            QTimer.singleShot(10, self.view_widget.canvas_changed)


class PickerLineTool(QgsMapTool):
    def __init__(self, view_widget):
        QgsMapTool.__init__(self, view_widget.render_widget.canvas)
        self.view_widget = view_widget
        # status rec icon and focus
        self.view_widget.status_LinesPicker.setEnabled(True)
        self.view_widget.status_LinesPicker.clicked.connect(self.finish)
        self.view_widget.render_widget.canvas.setFocus()

        self.start_new_line()

    def start_new_line(self):
        # set rubber band style
        color = QColor("red")
        color.setAlpha(40)
        # create the main line
        self.line = QgsRubberBand(self.view_widget.render_widget.canvas, QgsWkbTypes.LineGeometry)
        self.line.setColor(color)
        self.line.setWidth(3)
        # create the mouse/tmp line, this is main line + current mouse position
        self.aux_line = QgsRubberBand(self.view_widget.render_widget.canvas, QgsWkbTypes.LineGeometry)
        self.aux_line.setColor(color)
        self.aux_line.setWidth(3)

    def finish(self):
        if self.line:
            self.line.reset(QgsWkbTypes.LineGeometry)
        if self.aux_line:
            self.aux_line.reset(QgsWkbTypes.LineGeometry)
        self.line = None
        self.aux_line = None
        self.view_widget.status_LinesPicker.setDisabled(True)
        # restart point tool
        self.clean()
        self.view_widget.render_widget.canvas.unsetMapTool(self)
        self.view_widget.render_widget.canvas.setMapTool(self.view_widget.render_widget.default_point_tool)

    def define_line(self):
        # clean the aux line
        self.aux_line.reset(QgsWkbTypes.LineGeometry)
        self.aux_line = None
        # adjust the color
        color = QColor("red")
        color.setAlpha(80)
        self.line.setColor(color)
        self.line.setWidth(3)
        # save
        new_feature = QgsFeature()
        new_feature.setGeometry(self.line.asGeometry())
        self.view_widget.lines_drawn.append(self.line)
        # edit pixels in line
        QTimer.singleShot(180, lambda: self.edit(new_feature))

        self.start_new_line()

    def edit(self, new_feature):
        line_buffer = float(self.view_widget.LineBuffer.currentText())
        status = LayerToEdit.current.edit_from_line_picker(new_feature, line_buffer)
        if status:
            self.view_widget.render_widget.canvas.clearCache()
            self.view_widget.render_widget.canvas.refresh()
            # update status of undo/redo/clean buttons
            self.view_widget.UndoLine.setEnabled(LayerToEdit.current.history_lines.can_be_undone())
            self.view_widget.RedoLine.setEnabled(LayerToEdit.current.history_lines.can_be_redone())
            self.view_widget.CleanAllLines.setEnabled(len(self.view_widget.lines_drawn) > 0)

    def canvasMoveEvent(self, event):
        # highlight the current pixel value from mouse picker
        if self.view_widget.mousePixelValue2Table.isChecked():
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
            pixel_value_to_select = LayerToEdit.current.data_provider.identify(point, QgsRaster.IdentifyFormatValue).results()[1]
            if pixel_value_to_select is not None:
                LayerToEdit.current.select_value_in_recode_pixel_table(pixel_value_to_select)
        # draw the auxiliary line
        if self.aux_line is None:
            return
        if self.aux_line and self.aux_line.numberOfVertices():
            x = event.pos().x()
            y = event.pos().y()
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
            self.aux_line.removeLastPoint()
            self.aux_line.addPoint(point)

    def wheelEvent(self, event):
        QgsMapTool.wheelEvent(self, event)
        QTimer.singleShot(10, self.view_widget.canvas_changed)

    def canvasPressEvent(self, event):
        if self.line is None:
            self.finish()
            return
        # new point on line
        if event.button() == Qt.LeftButton:
            x = event.pos().x()
            y = event.pos().y()
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
            self.line.addPoint(point)
            self.aux_line.addPoint(point)
        # edit
        if event.button() == Qt.RightButton:
            if self.line and self.line.numberOfVertices():
                if self.line.numberOfVertices() < 2:
                    return
                # save line and edit
                self.define_line()

    def keyPressEvent(self, event):
        # edit
        if event.key() == Qt.Key_Enter or event.key() == Qt.Key_Return:
            if self.line and self.line.numberOfVertices():
                if self.line.numberOfVertices() < 2:
                    return
                # save line and edit
                self.define_line()
        # delete last point
        if event.key() == Qt.Key_Backspace or event.key() == Qt.Key_Delete:
            self.line.removeLastPoint()
            self.aux_line.removeLastPoint()
        # delete and finish
        if event.key() == Qt.Key_Escape:
            self.line.reset(QgsWkbTypes.LineGeometry)
            self.aux_line.reset(QgsWkbTypes.LineGeometry)
            self.finish()

    def keyReleaseEvent(self, event):
        if event.key() in [Qt.Key_Up, Qt.Key_Down, Qt.Key_Right, Qt.Key_Left, Qt.Key_PageUp, Qt.Key_PageDown]:
            QTimer.singleShot(10, self.view_widget.canvas_changed)


class PickerPolygonTool(QgsMapTool):
    def __init__(self, view_widget):
        QgsMapTool.__init__(self, view_widget.render_widget.canvas)
        self.view_widget = view_widget
        # status rec icon and focus
        self.view_widget.status_PolygonsPicker.setEnabled(True)
        self.view_widget.status_PolygonsPicker.clicked.connect(self.finish)
        self.view_widget.render_widget.canvas.setFocus()

        self.start_new_polygon()

    def start_new_polygon(self):
        # set rubber band style
        color = QColor("red")
        color.setAlpha(25)
        # create the main polygon rubber band
        self.rubber_band = QgsRubberBand(self.view_widget.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(color)
        self.rubber_band.setWidth(2)
        # create the mouse/tmp polygon rubber band, this is main rubber band + current mouse position
        self.aux_rubber_band = QgsRubberBand(self.view_widget.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
        self.aux_rubber_band.setColor(color)
        self.aux_rubber_band.setWidth(2)

    def finish(self):
        if self.rubber_band:
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        if self.aux_rubber_band:
            self.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.rubber_band = None
        self.aux_rubber_band = None
        self.view_widget.status_PolygonsPicker.setDisabled(True)
        # restart point tool
        self.clean()
        self.view_widget.render_widget.canvas.unsetMapTool(self)
        self.view_widget.render_widget.canvas.setMapTool(self.view_widget.render_widget.default_point_tool)

    def define_polygon(self):
        # clean the aux rubber band
        self.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.aux_rubber_band = None
        # adjust the color
        color = QColor("red")
        color.setAlpha(50)
        self.rubber_band.setColor(color)
        self.rubber_band.setWidth(2)
        # save
        new_feature = QgsFeature()
        new_feature.setGeometry(self.rubber_band.asGeometry())
        self.view_widget.polygons_drawn.append(self.rubber_band)
        # edit pixels inside polygon
        QTimer.singleShot(180, lambda: self.edit(new_feature))

        self.start_new_polygon()

    def edit(self, new_feature):
        status = LayerToEdit.current.edit_from_polygon_picker(new_feature)
        if status:
            self.view_widget.render_widget.canvas.clearCache()
            self.view_widget.render_widget.canvas.refresh()
            # update status of undo/redo/clean buttons
            self.view_widget.UndoPolygon.setEnabled(LayerToEdit.current.history_polygons.can_be_undone())
            self.view_widget.RedoPolygon.setEnabled(LayerToEdit.current.history_polygons.can_be_redone())
            self.view_widget.CleanAllPolygons.setEnabled(len(self.view_widget.polygons_drawn) > 0)

    def canvasMoveEvent(self, event):
        # highlight the current pixel value from mouse picker
        if self.view_widget.mousePixelValue2Table.isChecked():
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
            pixel_value_to_select = LayerToEdit.current.data_provider.identify(point, QgsRaster.IdentifyFormatValue).results()[1]
            if pixel_value_to_select is not None:
                LayerToEdit.current.select_value_in_recode_pixel_table(pixel_value_to_select)
        # draw the auxiliary rubber band
        if self.aux_rubber_band is None:
            return
        if self.aux_rubber_band and self.aux_rubber_band.numberOfVertices():
            x = event.pos().x()
            y = event.pos().y()
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
            self.aux_rubber_band.removeLastPoint()
            self.aux_rubber_band.addPoint(point)

    def wheelEvent(self, event):
        QgsMapTool.wheelEvent(self, event)
        QTimer.singleShot(10, self.view_widget.canvas_changed)

    def canvasPressEvent(self, event):
        if self.rubber_band is None:
            self.finish()
            return
        # new point on polygon
        if event.button() == Qt.LeftButton:
            x = event.pos().x()
            y = event.pos().y()
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
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
                # save polygon and edit
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

    def keyReleaseEvent(self, event):
        if event.key() in [Qt.Key_Up, Qt.Key_Down, Qt.Key_Right, Qt.Key_Left, Qt.Key_PageUp, Qt.Key_PageDown]:
            QTimer.singleShot(10, self.view_widget.canvas_changed)
