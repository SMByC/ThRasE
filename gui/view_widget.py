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
import uuid
from pathlib import Path

from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QWidget, QColorDialog
from qgis.PyQt.QtCore import Qt, pyqtSlot, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsWkbTypes, QgsFeature, QgsRaster
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.utils import iface

from ThRasE.core.editing import Pixel, LayerToEdit, edit_layer, check_before_editing
from ThRasE.utils.qgis_utils import get_pixel_centroid
from ThRasE.utils.system_utils import block_signals_to, wait_process

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))


class ViewWidget(QWidget):

    def setup_view_widget(self):
        self.render_widget.parent_view = self

        # ### init the three layer toolbars ###
        self.layer_toolbar_widget_1.setup_gui(1, self)
        self.layer_toolbar_widget_2.setup_gui(2, self)
        self.layer_toolbar_widget_3.setup_gui(3, self)
        # save layer toolbars in render widget
        self.render_widget.layer_toolbars = self.layer_toolbars
        # action for synchronize all view extent
        self.render_widget.canvas.extentsChanged.connect(self.canvas_changed)

        # ### init the editing tools ###
        # unselect items in recode pixel table
        self.mousePixelValue2Table.clicked.connect(self.unhighlight_cells_in_recode_pixel_table)

        # picker pixel tool edit
        self.widget_EditingToolbar.setEnabled(False)
        self.PixelsPicker.clicked.connect(self.use_pixels_picker_for_edit)
        # undo/redo
        self.UndoPixel.clicked.connect(lambda: self.go_to_history("undo", "pixel"))
        self.RedoPixel.clicked.connect(lambda: self.go_to_history("redo", "pixel"))

        # picker line tool edit
        self.lines_drawn = []
        self.lines_color = QColor("red")
        self.LinesColor.clicked.connect(self.change_lines_color)
        self.LinesPicker.clicked.connect(self.use_lines_picker_for_edit)
        # undo/redo
        self.UndoLine.clicked.connect(lambda: self.go_to_history("undo", "line"))
        self.RedoLine.clicked.connect(lambda: self.go_to_history("redo", "line"))
        # clean actions
        self.ClearAllLines.clicked.connect(self.clear_all_lines_drawn)

        # picker polygon tool edit
        self.polygons_drawn = []
        self.polygons_color = QColor("red")
        self.PolygonsColor.clicked.connect(self.change_polygons_color)
        self.PolygonsPicker.clicked.connect(self.use_polygons_picker_for_edit)
        # undo/redo
        self.UndoPolygon.clicked.connect(lambda: self.go_to_history("undo", "polygon"))
        self.RedoPolygon.clicked.connect(lambda: self.go_to_history("redo", "polygon"))
        # clean actions
        self.ClearAllPolygons.clicked.connect(self.clear_all_polygons_drawn)

        # picker freehand tool edit
        self.freehand_drawn = []
        self.freehand_color = QColor("red")
        self.FreehandColor.clicked.connect(self.change_freehand_color)
        self.FreehandPicker.clicked.connect(self.use_freehand_picker_for_edit)
        # undo/redo
        self.UndoFreehand.clicked.connect(lambda: self.go_to_history("undo", "freehand"))
        self.RedoFreehand.clicked.connect(lambda: self.go_to_history("redo", "freehand"))
        # clean actions
        self.ClearAllFreehand.clicked.connect(self.clear_all_freehand_drawn)

    @staticmethod
    @pyqtSlot()
    def unhighlight_cells_in_recode_pixel_table():
        from ThRasE.thrase import ThRasE
        with block_signals_to(ThRasE.dialog.recodePixelTable):
            [ThRasE.dialog.recodePixelTable.item(idx, 2).setBackground(Qt.white)
             for idx in range(len(LayerToEdit.current.pixels))]

    def update(self):
        valid_layers = [layer_toolbar.layer for layer_toolbar in self.layer_toolbars if layer_toolbar.is_active]
        if len(valid_layers) > 0:
            self.enable()
        else:
            self.disable()

    def enable(self):
        with block_signals_to(self.render_widget):
            # activate some parts of this view
            self.render_widget.setEnabled(True)
            self.render_widget.canvas.setCanvasColor(QColor(255, 255, 255))
            # set status for view widget
            self.is_active = True
            # if the navigation is using, then draw the current tile in this view
            from ThRasE.thrase import ThRasE
            if LayerToEdit.current is not None and LayerToEdit.current.navigation.is_valid \
               and ThRasE.dialog.currentTileKeepVisible.isChecked():
                LayerToEdit.current.navigation.current_tile.show()

    def disable(self):
        with block_signals_to(self.render_widget):
            self.render_widget.canvas.setLayers([])
            # deactivate some parts of this view
            self.render_widget.setDisabled(True)
            self.render_widget.canvas.setCanvasColor(QColor(245, 245, 245))
            # set status for view widget
            self.is_active = False
            # if the navigation is using, erase the current tile in this view
            from ThRasE.thrase import ThRasE
            if LayerToEdit.current is not None and LayerToEdit.current.navigation.is_valid:
                if ThRasE.dialog.currentTileKeepVisible.isChecked():
                    LayerToEdit.current.navigation.current_tile.show()
                else:
                    LayerToEdit.current.navigation.current_tile.hide()

            self.render_widget.refresh()

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

    @pyqtSlot()
    def change_lines_color(self, color=None):
        if not color:
            color = QColorDialog.getColor(self.lines_color, self)
        if color.isValid():
            self.lines_color = color
            self.LinesColor.setStyleSheet("QToolButton{{background-color:{};}}".format(color.name()))
            # restart a start draw again
            maptool_instance = self.render_widget.canvas.mapTool()
            if isinstance(maptool_instance, PickerLineTool):
                if maptool_instance.line:
                    maptool_instance.line.reset(QgsWkbTypes.LineGeometry)
                if maptool_instance.aux_line:
                    maptool_instance.aux_line.reset(QgsWkbTypes.LineGeometry)
                maptool_instance.start_new_line()

    @pyqtSlot()
    def change_polygons_color(self, color=None):
        if not color:
            color = QColorDialog.getColor(self.polygons_color, self)
        if color.isValid():
            self.polygons_color = color
            self.PolygonsColor.setStyleSheet("QToolButton{{background-color:{};}}".format(color.name()))
            # restart a start draw again
            maptool_instance = self.render_widget.canvas.mapTool()
            if isinstance(maptool_instance, PickerPolygonTool):
                if maptool_instance.rubber_band:
                    maptool_instance.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                if maptool_instance.aux_rubber_band:
                    maptool_instance.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                maptool_instance.start_new_polygon()

    @pyqtSlot()
    def change_freehand_color(self, color=None):
        if not color:
            color = QColorDialog.getColor(self.freehand_color, self)
        if color.isValid():
            self.freehand_color = color
            self.FreehandColor.setStyleSheet("QToolButton{{background-color:{};}}".format(color.name()))
            # restart a start draw again
            maptool_instance = self.render_widget.canvas.mapTool()
            if isinstance(maptool_instance, PickerFreehandTool):
                if maptool_instance.rubber_band:
                    maptool_instance.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                maptool_instance.start_new_freehand()

    @wait_process
    @edit_layer
    def go_to_history(self, action, from_edit_tool):
        from ThRasE.thrase import ThRasE

        if from_edit_tool == "pixel":
            if action == "undo":
                self.UndoPixel.setEnabled(False)
                pixel, value = LayerToEdit.current.pixel_edit_logs.undo()
                ThRasE.dialog.editing_status.setText("Undo: 1 pixel restored!")
            if action == "redo":
                self.RedoPixel.setEnabled(False)
                pixel, value = LayerToEdit.current.pixel_edit_logs.redo()
                ThRasE.dialog.editing_status.setText("Redo: 1 pixel remade!")
            # make action
            group_id = uuid.uuid4()
            LayerToEdit.current.edit_pixel(pixel, value, group_id)
            # refresh registry widget
            ThRasE.dialog.registry_widget.update_and_go_to_last()
            # update status of undo/redo buttons
            QTimer.singleShot(30, lambda: self.UndoPixel.setEnabled(LayerToEdit.current.pixel_edit_logs.can_be_undone()))
            QTimer.singleShot(30, lambda: self.RedoPixel.setEnabled(LayerToEdit.current.pixel_edit_logs.can_be_redone()))

        if from_edit_tool == "line":
            if action == "undo":
                self.UndoLine.setEnabled(False)
                line_feature, pixel_values = LayerToEdit.current.line_edit_logs.undo()
                # delete the line
                rubber_band = next((rb for rb in self.lines_drawn if
                                    rb.asGeometry().equals(line_feature.geometry())), None)
                if rubber_band:
                    rubber_band.reset(QgsWkbTypes.LineGeometry)
                    self.lines_drawn.remove(rubber_band)
                ThRasE.dialog.editing_status.setText("Undo: {} pixels restored!".format(len(pixel_values)))
            if action == "redo":
                self.RedoLine.setEnabled(False)
                line_feature, pixel_values = LayerToEdit.current.line_edit_logs.redo()
                # create, repaint and save the rubber band to redo
                rubber_band = QgsRubberBand(self.render_widget.canvas, QgsWkbTypes.LineGeometry)
                color = self.lines_color
                color.setAlpha(140)
                rubber_band.setColor(color)
                rubber_band.setWidth(4)
                rubber_band.addGeometry(line_feature.geometry())
                self.lines_drawn.append(rubber_band)
                ThRasE.dialog.editing_status.setText("Redo: {} pixels remade!".format(len(pixel_values)))
            # make action
            group_id = uuid.uuid4()
            [LayerToEdit.current.edit_pixel(pixel, value, group_id) for pixel, value in pixel_values]
            # refresh registry widget
            ThRasE.dialog.registry_widget.update_and_go_to_last()
            # update status of undo/redo/clean buttons
            QTimer.singleShot(50, lambda: self.UndoLine.setEnabled(LayerToEdit.current.line_edit_logs.can_be_undone()))
            QTimer.singleShot(50, lambda: self.RedoLine.setEnabled(LayerToEdit.current.line_edit_logs.can_be_redone()))
            self.ClearAllLines.setEnabled(len(self.lines_drawn) > 0)

        if from_edit_tool == "polygon":
            if action == "undo":
                self.UndoPolygon.setEnabled(False)
                polygon_feature, pixel_values = LayerToEdit.current.polygon_edit_logs.undo()
                # delete the rubber band
                rubber_band = next((rb for rb in self.polygons_drawn if
                                    rb.asGeometry().equals(polygon_feature.geometry())), None)
                if rubber_band:
                    rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                    self.polygons_drawn.remove(rubber_band)
                ThRasE.dialog.editing_status.setText("Undo: {} pixels restored!".format(len(pixel_values)))
            if action == "redo":
                self.RedoPolygon.setEnabled(False)
                polygon_feature, pixel_values = LayerToEdit.current.polygon_edit_logs.redo()
                # create, repaint and save the rubber band to redo
                rubber_band = QgsRubberBand(self.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
                color = self.polygons_color
                color.setAlpha(140)
                rubber_band.setColor(color)
                rubber_band.setWidth(4)
                rubber_band.addGeometry(polygon_feature.geometry())
                self.polygons_drawn.append(rubber_band)
                ThRasE.dialog.editing_status.setText("Redo: {} pixels remade!".format(len(pixel_values)))
            # make action
            group_id = uuid.uuid4()
            [LayerToEdit.current.edit_pixel(pixel, value, group_id) for pixel, value in pixel_values]
            # refresh registry widget
            ThRasE.dialog.registry_widget.update_and_go_to_last()
            # update status of undo/redo buttons
            QTimer.singleShot(50, lambda: self.UndoPolygon.setEnabled(LayerToEdit.current.polygon_edit_logs.can_be_undone()))
            QTimer.singleShot(50, lambda: self.RedoPolygon.setEnabled(LayerToEdit.current.polygon_edit_logs.can_be_redone()))
            self.ClearAllPolygons.setEnabled(len(self.polygons_drawn) > 0)

        if from_edit_tool == "freehand":
            if action == "undo":
                self.UndoFreehand.setEnabled(False)
                freehand_feature, pixel_values = LayerToEdit.current.freehand_edit_logs.undo()
                # delete the rubber band
                rubber_band = next((rb for rb in self.freehand_drawn if
                                    rb.asGeometry().equals(freehand_feature.geometry())), None)
                if rubber_band:
                    rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                    self.freehand_drawn.remove(rubber_band)
                ThRasE.dialog.editing_status.setText("Undo: {} pixels restored!".format(len(pixel_values)))
            if action == "redo":
                self.RedoFreehand.setEnabled(False)
                freehand_feature, pixel_values = LayerToEdit.current.freehand_edit_logs.redo()
                # create, repaint and save the rubber band to redo
                rubber_band = QgsRubberBand(self.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
                color = self.freehand_color
                color.setAlpha(140)
                rubber_band.setColor(color)
                rubber_band.setWidth(4)
                rubber_band.addGeometry(freehand_feature.geometry())
                self.freehand_drawn.append(rubber_band)
                ThRasE.dialog.editing_status.setText("Redo: {} pixels remade!".format(len(pixel_values)))
            # make action
            group_id = uuid.uuid4()
            [LayerToEdit.current.edit_pixel(pixel, value, group_id) for pixel, value in pixel_values]
            # refresh registry widget
            ThRasE.dialog.registry_widget.update_and_go_to_last()
            # update status of undo/redo buttons
            QTimer.singleShot(50, lambda: self.UndoFreehand.setEnabled(LayerToEdit.current.freehand_edit_logs.can_be_undone()))
            QTimer.singleShot(50, lambda: self.RedoFreehand.setEnabled(LayerToEdit.current.freehand_edit_logs.can_be_redone()))
            self.ClearAllFreehand.setEnabled(len(self.freehand_drawn) > 0)
        # update changes done in the layer and view
        self.render_widget.refresh()

    @staticmethod
    @pyqtSlot()
    def toggle_layer_toolbars(enable=None):
        # open/close all layer toolbars widgets
        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            if enable is None:
                view_widget.layer_toolbar_widgets.setHidden(view_widget.layer_toolbar_widgets.isVisible())
            else:
                view_widget.layer_toolbar_widgets.setVisible(enable)

        # refresh all extents based on the first active view
        actives_view_widget = [view_widget for view_widget in ThRasEDialog.view_widgets if view_widget.is_active]
        if actives_view_widget:
            QTimer.singleShot(10, lambda: actives_view_widget[0].canvas_changed())

    @staticmethod
    @pyqtSlot()
    def toggle_editing_toolbars(enable=None):
        # open/close all edition tool widgets
        from ThRasE.gui.main_dialog import ThRasEDialog
        for view_widget in ThRasEDialog.view_widgets:
            if enable is None:
                view_widget.widget_EditingToolbar.setHidden(view_widget.widget_EditingToolbar.isVisible())
            else:
                view_widget.widget_EditingToolbar.setVisible(enable)

        # refresh all extents based on the first active view
        actives_view_widget = [view_widget for view_widget in ThRasEDialog.view_widgets if view_widget.is_active]
        if actives_view_widget:
            QTimer.singleShot(10, lambda: actives_view_widget[0].canvas_changed())

    @pyqtSlot()
    def use_pixels_picker_for_edit(self):
        from ThRasE.thrase import ThRasE
        if isinstance(self.render_widget.canvas.mapTool(), PickerPixelTool):
            # disable edit and return to normal map tool
            self.render_widget.canvas.mapTool().finish()
            ThRasE.dialog.editing_status.setText("")
        else:
            if not check_before_editing():
                self.PixelsPicker.setChecked(False)
                return
            # finish the other picker activation
            if isinstance(self.render_widget.canvas.mapTool(), (PickerLineTool, PickerPolygonTool, PickerFreehandTool)):
                self.render_widget.canvas.mapTool().finish()
            # enable edit
            self.render_widget.canvas.setMapTool(PickerPixelTool(self), clean=True)

    @pyqtSlot()
    def use_lines_picker_for_edit(self):
        from ThRasE.thrase import ThRasE
        if isinstance(self.render_widget.canvas.mapTool(), PickerLineTool):
            # disable edit and return to normal map tool
            self.render_widget.canvas.mapTool().finish()
            ThRasE.dialog.editing_status.setText("")
        else:
            if not check_before_editing():
                self.LinesPicker.setChecked(False)
                return
            # finish the other picker activation
            if isinstance(self.render_widget.canvas.mapTool(), (PickerPixelTool, PickerPolygonTool, PickerFreehandTool)):
                self.render_widget.canvas.mapTool().finish()
            # enable edit
            self.render_widget.canvas.setMapTool(PickerLineTool(self), clean=True)

    @pyqtSlot()
    def use_polygons_picker_for_edit(self):
        from ThRasE.thrase import ThRasE
        if isinstance(self.render_widget.canvas.mapTool(), PickerPolygonTool):
            # disable edit and return to normal map tool
            self.render_widget.canvas.mapTool().finish()
            ThRasE.dialog.editing_status.setText("")
        else:
            if not check_before_editing():
                self.PolygonsPicker.setChecked(False)
                return
            # finish the other picker activation
            if isinstance(self.render_widget.canvas.mapTool(), (PickerPixelTool, PickerLineTool, PickerFreehandTool)):
                self.render_widget.canvas.mapTool().finish()
            # enable edit
            self.render_widget.canvas.setMapTool(PickerPolygonTool(self), clean=True)

    @pyqtSlot()
    def use_freehand_picker_for_edit(self):
        from ThRasE.thrase import ThRasE
        if isinstance(self.render_widget.canvas.mapTool(), PickerFreehandTool):
            # disable edit and return to normal map tool
            self.render_widget.canvas.mapTool().finish()
            ThRasE.dialog.editing_status.setText("")
        else:
            if not check_before_editing():
                self.FreehandPicker.setChecked(False)
                return
            # finish the other picker activation
            if isinstance(self.render_widget.canvas.mapTool(), (PickerPixelTool, PickerLineTool, PickerPolygonTool)):
                self.render_widget.canvas.mapTool().finish()
            # enable edit
            self.render_widget.canvas.setMapTool(PickerFreehandTool(self), clean=True)

    @pyqtSlot()
    def clear_all_lines_drawn(self):
        # clean/reset all rubber bands
        for rubber_band in self.lines_drawn:
            rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.lines_drawn = []
        self.ClearAllLines.setEnabled(False)

    @pyqtSlot()
    def clear_all_polygons_drawn(self):
        # clean/reset all rubber bands
        for rubber_band in self.polygons_drawn:
            rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.polygons_drawn = []
        self.ClearAllPolygons.setEnabled(False)

    @pyqtSlot()
    def clear_all_freehand_drawn(self):
        # clean/reset all rubber bands
        for rubber_band in self.freehand_drawn:
            rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.freehand_drawn = []
        self.ClearAllFreehand.setEnabled(False)


# load a single view in the widget edition when columns == 1
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'view_widget_single.ui'))
class ViewWidgetSingle(ViewWidget, FORM_CLASS):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.id = None
        self.is_active = False
        self.layer_toolbars = []  # for save the layer toolbars instances
        self.setupUi(self)


# load a multi view (two rows) in the widget edition when columns > 1
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'view_widget_multi.ui'))
class ViewWidgetMulti(ViewWidget, FORM_CLASS):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.id = None
        self.is_active = False
        self.layer_toolbars = []  # for save the layer toolbars instances
        self.setupUi(self)


class PickerPixelTool(QgsMapTool):
    def __init__(self, view_widget):
        QgsMapTool.__init__(self, view_widget.render_widget.canvas)
        self.view_widget = view_widget
        # status rec icon and focus
        self.view_widget.render_widget.canvas.setFocus()

    def finish(self):
        self.view_widget.PixelsPicker.setChecked(False)
        self.view_widget.unhighlight_cells_in_recode_pixel_table()
        # restart point tool
        self.clean()
        self.view_widget.render_widget.canvas.unsetMapTool(self)
        self.view_widget.render_widget.canvas.setMapTool(self.view_widget.render_widget.default_point_tool)
        # clear map coordinate in the footer
        from ThRasE.thrase import ThRasE
        ThRasE.dialog.map_coordinate.setText("")

    def edit(self, event):
        x = event.pos().x()
        y = event.pos().y()
        point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
        pixel = Pixel(point=get_pixel_centroid(x=point.x(), y=point.y()))
        status = LayerToEdit.current.edit_from_pixel_picker(pixel)
        if status:
            self.view_widget.render_widget.refresh()
            # update status of undo/redo buttons
            self.view_widget.UndoPixel.setEnabled(LayerToEdit.current.pixel_edit_logs.can_be_undone())
            self.view_widget.RedoPixel.setEnabled(LayerToEdit.current.pixel_edit_logs.can_be_redone())

    def canvasMoveEvent(self, event):
        # set map coordinates in the footer
        from ThRasE.thrase import ThRasE
        map_coordinate = iface.mapCanvas().getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
        crs = iface.mapCanvas().mapSettings().destinationCrs().authid()
        ThRasE.dialog.map_coordinate.setText("Coordinate: {:.3f}, {:.3f} ({})".format(
            map_coordinate.x(), map_coordinate.y(), crs))

        # highlight the current pixel value from mouse picker
        if self.view_widget.mousePixelValue2Table.isChecked():
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
            pixel_value_to_select = \
                LayerToEdit.current.data_provider.identify(point, QgsRaster.IdentifyFormatValue).results()[LayerToEdit.current.band]
            LayerToEdit.current.highlight_value_in_recode_pixel_table(pixel_value_to_select)

    def canvasPressEvent(self, event):
        # edit the pixel over pointer mouse on left-click
        if event.button() == Qt.LeftButton:
            self.edit(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.finish()


class PickerLineTool(QgsMapTool):
    def __init__(self, view_widget):
        QgsMapTool.__init__(self, view_widget.render_widget.canvas)
        self.view_widget = view_widget
        # status rec icon and focus
        self.view_widget.render_widget.canvas.setFocus()

        self.start_new_line()

    def start_new_line(self):
        # set rubber band style
        color = self.view_widget.lines_color
        color.setAlpha(70)
        # create the main line
        self.line = QgsRubberBand(self.view_widget.render_widget.canvas, QgsWkbTypes.LineGeometry)
        self.line.setColor(color)
        self.line.setWidth(4)
        # create the mouse/tmp line, this is main line + current mouse position
        self.aux_line = QgsRubberBand(self.view_widget.render_widget.canvas, QgsWkbTypes.LineGeometry)
        self.aux_line.setColor(color)
        self.aux_line.setWidth(4)

    def finish(self):
        if self.line:
            self.line.reset(QgsWkbTypes.LineGeometry)
        if self.aux_line:
            self.aux_line.reset(QgsWkbTypes.LineGeometry)
        self.line = None
        self.aux_line = None
        self.view_widget.LinesPicker.setChecked(False)
        self.view_widget.unhighlight_cells_in_recode_pixel_table()
        # restart point tool
        self.clean()
        self.view_widget.render_widget.canvas.unsetMapTool(self)
        self.view_widget.render_widget.canvas.setMapTool(self.view_widget.render_widget.default_point_tool)
        # clear map coordinate in the footer
        from ThRasE.thrase import ThRasE
        ThRasE.dialog.map_coordinate.setText("")

    def define_line(self):
        # clean the aux line
        self.aux_line.reset(QgsWkbTypes.LineGeometry)
        self.aux_line = None
        # adjust the color
        color = self.view_widget.lines_color
        color.setAlpha(140)
        self.line.setColor(color)
        self.line.setWidth(4)
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
        if status:  # at least one pixel was edited
            self.view_widget.render_widget.refresh()
            # update status of undo/redo/clean buttons
            self.view_widget.UndoLine.setEnabled(LayerToEdit.current.line_edit_logs.can_be_undone())
            self.view_widget.RedoLine.setEnabled(LayerToEdit.current.line_edit_logs.can_be_redone())
            self.view_widget.ClearAllLines.setEnabled(len(self.view_widget.lines_drawn) > 0)
        else:
            self.line.reset(QgsWkbTypes.LineGeometry)
            rubber_band = self.view_widget.lines_drawn[-1]
            if rubber_band:
                rubber_band.reset(QgsWkbTypes.LineGeometry)
                self.view_widget.lines_drawn.remove(rubber_band)

    def canvasMoveEvent(self, event):
        # set map coordinates in the footer
        from ThRasE.thrase import ThRasE
        map_coordinate = iface.mapCanvas().getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
        crs = iface.mapCanvas().mapSettings().destinationCrs().authid()
        ThRasE.dialog.map_coordinate.setText("Coordinate: {:.3f}, {:.3f} ({})".format(
            map_coordinate.x(), map_coordinate.y(), crs))

        # highlight the current pixel value from mouse picker
        if self.view_widget.mousePixelValue2Table.isChecked():
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
            pixel_value_to_select = \
                LayerToEdit.current.data_provider.identify(point, QgsRaster.IdentifyFormatValue).results()[LayerToEdit.current.band]
            LayerToEdit.current.highlight_value_in_recode_pixel_table(pixel_value_to_select)
        # draw the auxiliary line
        if self.aux_line is None:
            return
        if self.aux_line and self.aux_line.numberOfVertices():
            x = event.pos().x()
            y = event.pos().y()
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
            self.aux_line.removeLastPoint()
            self.aux_line.addPoint(point)

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


class PickerPolygonTool(QgsMapTool):
    def __init__(self, view_widget):
        QgsMapTool.__init__(self, view_widget.render_widget.canvas)
        self.view_widget = view_widget
        # status rec icon and focus
        self.view_widget.render_widget.canvas.setFocus()

        self.start_new_polygon()

    def start_new_polygon(self):
        # set rubber band style
        color = self.view_widget.polygons_color
        color.setAlpha(70)
        # create the main polygon rubber band
        self.rubber_band = QgsRubberBand(self.view_widget.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(color)
        self.rubber_band.setWidth(4)
        # create the mouse/tmp polygon rubber band, this is main rubber band + current mouse position
        self.aux_rubber_band = QgsRubberBand(self.view_widget.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
        self.aux_rubber_band.setColor(color)
        self.aux_rubber_band.setWidth(4)

    def define_polygon(self):
        # clean the aux rubber band
        self.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.aux_rubber_band = None
        # adjust the color
        color = self.view_widget.polygons_color
        color.setAlpha(140)
        self.rubber_band.setColor(color)
        self.rubber_band.setWidth(4)
        # save
        new_feature = QgsFeature()
        new_feature.setGeometry(self.rubber_band.asGeometry())
        self.view_widget.polygons_drawn.append(self.rubber_band)
        # edit pixels inside polygon
        QTimer.singleShot(180, lambda: self.edit(new_feature))

        self.start_new_polygon()

    def edit(self, new_feature):
        status = LayerToEdit.current.edit_from_polygon_picker(new_feature)
        if status:  # at least one pixel was edited
            self.view_widget.render_widget.refresh()
            # update status of undo/redo/clean buttons
            self.view_widget.UndoPolygon.setEnabled(LayerToEdit.current.polygon_edit_logs.can_be_undone())
            self.view_widget.RedoPolygon.setEnabled(LayerToEdit.current.polygon_edit_logs.can_be_redone())
            self.view_widget.ClearAllPolygons.setEnabled(len(self.view_widget.polygons_drawn) > 0)
        else:
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            rubber_band = self.view_widget.polygons_drawn[-1]
            if rubber_band:
                rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                self.view_widget.polygons_drawn.remove(rubber_band)

    def canvasMoveEvent(self, event):
        # set map coordinates in the footer
        from ThRasE.thrase import ThRasE
        map_coordinate = iface.mapCanvas().getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
        crs = iface.mapCanvas().mapSettings().destinationCrs().authid()
        ThRasE.dialog.map_coordinate.setText("Coordinate: {:.3f}, {:.3f} ({})".format(
            map_coordinate.x(), map_coordinate.y(), crs))

        # highlight the current pixel value from mouse picker
        if self.view_widget.mousePixelValue2Table.isChecked():
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
            pixel_value_to_select = \
                LayerToEdit.current.data_provider.identify(point, QgsRaster.IdentifyFormatValue).results()[LayerToEdit.current.band]
            LayerToEdit.current.highlight_value_in_recode_pixel_table(pixel_value_to_select)
        # draw the auxiliary rubber band
        if self.aux_rubber_band is None:
            return
        if self.aux_rubber_band and self.aux_rubber_band.numberOfVertices():
            x = event.pos().x()
            y = event.pos().y()
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
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

    def finish(self):
        if self.rubber_band:
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        if self.aux_rubber_band:
            self.aux_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.rubber_band = None
        self.aux_rubber_band = None
        self.view_widget.PolygonsPicker.setChecked(False)
        self.view_widget.unhighlight_cells_in_recode_pixel_table()
        # restart point tool
        self.clean()
        self.view_widget.render_widget.canvas.unsetMapTool(self)
        self.view_widget.render_widget.canvas.setMapTool(self.view_widget.render_widget.default_point_tool)
        # clear map coordinate in the footer
        from ThRasE.thrase import ThRasE
        ThRasE.dialog.map_coordinate.setText("")


class PickerFreehandTool(QgsMapTool):

    def __init__(self, view_widget):
        QgsMapTool.__init__(self, view_widget.render_widget.canvas)
        self.view_widget = view_widget
        # status rec icon and focus
        self.view_widget.render_widget.canvas.setFocus()

        self.start_new_freehand()

    def start_new_freehand(self):
        # set rubber band style
        color = self.view_widget.freehand_color
        color.setAlpha(140)
        # create the main freehand rubber band
        self.rubber_band = QgsRubberBand(self.view_widget.render_widget.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(color)
        self.rubber_band.setWidth(4)
        self.drawing = False

    def keyPressEvent(self, event):
        # remove/ignore current freehand
        if self.drawing and (event.key() == Qt.Key_Backspace or event.key() == Qt.Key_Delete):
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.start_new_freehand()
        # delete and finish
        if event.key() == Qt.Key_Escape:
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.finish()

    def canvasPressEvent(self, event):
        if self.drawing:
            return

        # start new freehand draw
        if event.button() == Qt.LeftButton:
            self.drawing = True
            x = event.pos().x()
            y = event.pos().y()
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(x, y)
            self.rubber_band.addPoint(point)

    def canvasMoveEvent(self, event):
        # set map coordinates in the footer
        from ThRasE.thrase import ThRasE
        map_coordinate = iface.mapCanvas().getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
        crs = iface.mapCanvas().mapSettings().destinationCrs().authid()
        ThRasE.dialog.map_coordinate.setText("Coordinate: {:.3f}, {:.3f} ({})".format(
            map_coordinate.x(), map_coordinate.y(), crs))

        # highlight the current pixel value from mouse picker
        if self.view_widget.mousePixelValue2Table.isChecked():
            point = self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())
            pixel_value_to_select = \
                LayerToEdit.current.data_provider.identify(point, QgsRaster.IdentifyFormatValue).results()[LayerToEdit.current.band]
            LayerToEdit.current.highlight_value_in_recode_pixel_table(pixel_value_to_select)
        # draw the auxiliary rubber band
        if not self.drawing or not self.rubber_band:
            return
        self.rubber_band.addPoint(self.view_widget.render_widget.canvas.getCoordinateTransform().toMapCoordinates(event.pos()))

    def canvasReleaseEvent(self, event):
        self.drawing = False

        if not self.rubber_band:
            return

        if self.rubber_band.numberOfVertices() > 2:
            # save
            new_feature = QgsFeature()
            new_feature.setGeometry(self.rubber_band.asGeometry())
            self.view_widget.freehand_drawn.append(self.rubber_band)
            # edit pixels inside freehand polygon
            QTimer.singleShot(180, lambda: self.edit(new_feature))

        self.start_new_freehand()

    def edit(self, new_feature):
        status = LayerToEdit.current.edit_from_freehand_picker(new_feature)
        if status:  # at least one pixel was edited
            self.view_widget.render_widget.refresh()
            # update status of undo/redo/clean buttons
            self.view_widget.UndoFreehand.setEnabled(LayerToEdit.current.freehand_edit_logs.can_be_undone())
            self.view_widget.RedoFreehand.setEnabled(LayerToEdit.current.freehand_edit_logs.can_be_redone())
            self.view_widget.ClearAllFreehand.setEnabled(len(self.view_widget.freehand_drawn) > 0)
        else:
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            rubber_band = self.view_widget.freehand_drawn[-1]
            if rubber_band:
                rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                self.view_widget.freehand_drawn.remove(rubber_band)

    def finish(self):
        if self.rubber_band:
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

        self.rubber_band = None
        self.view_widget.FreehandPicker.setChecked(False)
        self.view_widget.unhighlight_cells_in_recode_pixel_table()
        # restart point tool
        self.clean()
        self.view_widget.render_widget.canvas.unsetMapTool(self)
        self.view_widget.render_widget.canvas.setMapTool(self.view_widget.render_widget.default_point_tool)
        # clear map coordinate in the footer
        from ThRasE.thrase import ThRasE
        ThRasE.dialog.map_coordinate.setText("")
