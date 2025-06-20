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
import configparser
import tempfile
from copy import deepcopy
from pathlib import Path
import yaml
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import pyqtSignal, Qt, pyqtSlot, QTimer
from qgis.PyQt.QtWidgets import QMessageBox, QGridLayout, QFileDialog, QTableWidgetItem, QColorDialog, QWidget, QLabel
from qgis.core import Qgis, QgsMapLayerProxyModel, QgsRectangle, QgsPointXY, QgsCoordinateReferenceSystem, \
    QgsCoordinateTransform, QgsProject
from qgis.PyQt.QtGui import QColor, QFont, QIcon
from qgis.utils import iface

from ThRasE.core.edition import LayerToEdit
from ThRasE.gui.about_dialog import AboutDialog
from ThRasE.gui.view_widget import ViewWidget, ViewWidgetSingle, ViewWidgetMulti
from ThRasE.gui.autofill_dialog import AutoFill
from ThRasE.gui.apply_from_thematic_classes import ApplyFromThematicClasses
from ThRasE.utils.qgis_utils import load_and_select_filepath_in, valid_file_selected_in, apply_symbology, \
    get_nodata_value, unset_the_nodata_value, get_file_path_of_layer, unload_layer, load_layer, \
    add_color_value_to_symbology
from ThRasE.utils.system_utils import block_signals_to, error_handler, wait_process, open_file

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'main_dialog.ui'))

# read metadata
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
        #
        self.grid_rows = None
        self.grid_columns = None
        # flags
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)

    def setup_gui(self):
        # ######### plugin info ######### #
        self.about_dialog = AboutDialog()
        self.QPBtn_PluginInfo.setToolTip("ThRasE v{}".format(VERSION))
        self.QPBtn_PluginInfo.clicked.connect(self.about_dialog.show)

        # ######### navigation ######### #
        self.NavigationBlockWidget.setHidden(True)
        self.NavigationBlockWidget.setDisabled(True)
        self.NavigationBlockWidgetControls.setDisabled(True)
        self.QPBtn_EnableNavigation.clicked.connect(self.enable_navigation_tool)
        self.QPBtn_OpenNavigationDialog.clicked.connect(self.open_navigation_dialog)
        self.QPBtn_ReloadRecodeTable.setDisabled(True)
        self.QPBtn_RestoreRecodeTable.setDisabled(True)
        self.QPBtn_AutoFill.setDisabled(True)
        self.Widget_GlobalEditingTools.setDisabled(True)
        self.currentTile.clicked.connect(self.go_to_current_tile)
        self.previousTile.clicked.connect(self.go_to_previous_tile)
        self.nextTile.clicked.connect(self.go_to_next_tile)
        self.currentTileKeepVisible.clicked.connect(self.current_tile_keep_visible)
        # open in Google Earth
        self.QPBtn_OpenInGE.clicked.connect(self.open_current_tile_navigation_in_google_engine)

        # ######### build the view render widgets windows ######### #
        self.init_dialog = InitDialog()
        if self.init_dialog.exec_():
            # new
            if self.init_dialog.tabWidget.currentIndex() == 0:
                settings_type = "new"
                self.grid_rows = self.init_dialog.grid_rows.value()
                self.grid_columns = self.init_dialog.grid_columns.value()
            # load
            if self.init_dialog.tabWidget.currentIndex() == 1:
                settings_type = "load"
                yaml_file_path = self.init_dialog.QgsFile_LoadConfigFile.filePath()
                yaml_config = self.get_yaml_config(yaml_file_path)
                if yaml_config:
                    # restore rows/columns
                    self.grid_rows = yaml_config["grid_view_widgets"]["rows"]
                    self.grid_columns = yaml_config["grid_view_widgets"]["columns"]
                else:
                    return False
        else:
            self.close()
            return False

        # configure the views layout
        views_layout = QGridLayout()
        views_layout.setSpacing(0)
        views_layout.setMargin(0)
        view_widgets = []
        for row in range(self.grid_rows):
            for column in range(self.grid_columns):
                if self.grid_columns == 1:
                    new_view_widget = ViewWidgetSingle()
                else:
                    new_view_widget = ViewWidgetMulti()
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
        self.QCBox_band_LayerToEdit.currentIndexChanged.connect(lambda: self.setup_layer_to_edit())
        # call to browse the render file
        self.QPBtn_browseLayerToEdit.clicked.connect(self.browse_dialog_layer_to_edit)
        # update recode pixel table
        self.recodePixelTable.itemChanged.connect(self.update_recode_pixel_table)
        # for change the class color
        self.recodePixelTable.itemClicked.connect(self.table_item_clicked)

        # ######### setup active layers and edit tools ######### #
        self.QPBtn_ActiveLayers.clicked.connect(ViewWidget.active_layers_widget)
        self.QPBtn_EditionTools.clicked.connect(ViewWidget.edition_tools_widget)
        self.QCBox_NoActiveLayers.currentIndexChanged[int].connect(self.set_active_layers)

        # ######### others ######### #
        self.QPBtn_ReloadRecodeTable.clicked.connect(self.reload_recode_table)
        self.QPBtn_RestoreRecodeTable.clicked.connect(self.restore_recode_table)
        self.autofill_dialog = AutoFill()
        self.QPBtn_AutoFill.clicked.connect(self.open_autofill_dialog)
        self.QGBox_GlobalEditTools.setHidden(True)
        self.QPBtn_ApplyWholeImage.clicked.connect(self.apply_whole_image)
        self.apply_from_thematic_classes = ApplyFromThematicClasses()
        self.QPBtn_ApplyFromThematicClasses.clicked.connect(self.apply_from_thematic_classes_dialog)
        self.SaveConfig.clicked.connect(self.file_dialog_save_thrase_config)

        # ######### CCD plugin widget ######### #
        # set up the Continuous Change Detection widget
        self.QPBtn_CCDPlugin.clicked.connect(self.ccd_plugin_widget)
        self.widget_ccd.setVisible(False)
        ccd_widget_layout = self.widget_ccd.layout()
        try:
            from qgis.PyQt.QtWebKit import QWebSettings  # check first if the QtWebKit is available in QT5 client
            from CCD_Plugin.CCD_Plugin import CCD_Plugin
            from CCD_Plugin.gui.CCD_Plugin_dockwidget import CCD_PluginDockWidget
            from CCD_Plugin.utils.config import get_plugin_config, restore_plugin_config
            self.ccd_plugin_available = True
        except ImportError:
            label = QLabel("\nError: Continuous Change Detection (CCD) plugin is not available in your Qgis\n"
                           "instance. To integrate the CCD inside ThRasE, go to the plugin managing in\n"
                           "Qgis and search CCD-Plugin, install it and restart ThRasE.\n\n"
                           "CCD helps to analyze the trends and breakpoints of change of the\n"
                           "samples over multi-year time series using Landsat and Sentinel.\n", self)
            label.setAlignment(Qt.AlignCenter)
            ccd_widget_layout.addWidget(label)
            ccd_widget_layout.setAlignment(Qt.AlignCenter)
            self.ccd_plugin_available = False

        if self.ccd_plugin_available:
            # Remove all widgets from the layout
            for i in reversed(range(ccd_widget_layout.count())):
                ccd_widget_layout.itemAt(i).widget().setParent(None)

            # create the ccd widget
            self.ccd_plugin = CCD_Plugin(iface)
            view_canvas = [view_widget.render_widget.canvas for view_widget in ThRasEDialog.view_widgets]
            self.ccd_plugin.widget = CCD_PluginDockWidget(id=self.ccd_plugin.id, canvas=view_canvas, parent=self)
            # adjust the dockwidget (ccd_widget) as a normal widget
            self.ccd_plugin.widget.setWindowFlags(Qt.Widget)
            self.ccd_plugin.widget.setWindowFlag(Qt.WindowCloseButtonHint, False)
            self.ccd_plugin.widget.setFloating(False)
            self.ccd_plugin.widget.setTitleBarWidget(QWidget(None))
            # init tmp dir for all process and intermediate files
            from ThRasE.thrase import ThRasE
            if ThRasE.tmp_dir is None:
                ThRasE.tmp_dir = tempfile.mkdtemp()
            self.ccd_plugin.tmp_dir = ThRasE.tmp_dir
            # replace the "widget_ccd" UI widget inside ThRasE window with the ccd widget
            ccd_widget_layout.insertWidget(0, self.ccd_plugin.widget)
            ccd_widget_layout.update()

            # uncheck the editing tools enabled in ThRasE when CCD plugin is active
            def coordinates_from_map(checked):
                if checked:
                    for view_widget in ThRasEDialog.view_widgets:
                        view_widget.PixelsPicker.setChecked(False)
                        view_widget.LinesPicker.setChecked(False)
                        view_widget.PolygonsPicker.setChecked(False)
                        view_widget.FreehandPicker.setChecked(False)
                        ThRasE.dialog.editing_status.setText("")
                        ThRasE.dialog.map_coordinate.setText("")
            self.ccd_plugin.widget.pick_on_map.toggled.connect(coordinates_from_map)

            # uncheck the pick on map button in CCD plugin when editing tools are enabled
            for view_widget in ThRasEDialog.view_widgets:
                view_widget.PixelsPicker.clicked.connect(lambda: self.ccd_plugin.widget.pick_on_map.setChecked(False))
                view_widget.LinesPicker.clicked.connect(lambda: self.ccd_plugin.widget.pick_on_map.setChecked(False))
                view_widget.PolygonsPicker.clicked.connect(lambda: self.ccd_plugin.widget.pick_on_map.setChecked(False))
                view_widget.FreehandPicker.clicked.connect(lambda: self.ccd_plugin.widget.pick_on_map.setChecked(False))

        # ######### set the default values ######### #
        if settings_type == "new":
            # set the default active layers
            self.set_active_layers(self.QCBox_NoActiveLayers.currentIndex())

        # ######### load settings from file ######### #
        if settings_type == "load":
            self.restore_config(yaml_file_path, yaml_config)

        return True

    def set_active_layers(self, index):
        num_active_layers = index + 1
        for view_widget in ThRasEDialog.view_widgets:
            for active_layer in view_widget.active_layers:
                if active_layer.id <= num_active_layers:
                    active_layer.activate()
                else:
                    active_layer.deactivate()

    def get_yaml_config(self, yaml_file_path):
        if yaml_file_path != '' and os.path.isfile(yaml_file_path) and os.access(yaml_file_path, os.R_OK):
            # load classification from yaml file
            with open(yaml_file_path, 'r') as yaml_file:
                try:
                    yaml_config = yaml.load(yaml_file, Loader=Loader)
                    return yaml_config
                except Exception as err:
                    msg = "Error while read the yaml file ThRasE configuration:\n\n{}".format(err)
                    QMessageBox.critical(self.init_dialog, 'ThRasE - Loading config...', msg, QMessageBox.Ok)
                    self.close()
                    return False
        else:
            msg = "Opening the config ThRasE file:\n{}\n\nFile does not exist, or you do not " \
                  "have read access to the file".format(self.init_dialog.QgsFile_LoadConfigFile.filePath())
            QMessageBox.critical(self.init_dialog, 'ThRasE - Loading config...', msg, QMessageBox.Ok)
            self.close()
            return False

    @wait_process
    def restore_config(self, yaml_file_path, yaml_config):
        def get_restore_path(_path):
            """check if the file path exists or try using relative path to the yml file"""
            if _path is None:
                return None
            if not os.path.isfile(_path):
                _rel_path = os.path.join(os.path.dirname(yaml_file_path), _path)
                if os.path.isfile(_rel_path):
                    # the path is relative to the yml file
                    return os.path.abspath(_rel_path)
            return _path


        # dialog size
        if "main_dialog_size" in yaml_config:
            self.resize(*yaml_config["main_dialog_size"])
        # setup the thematic file to edit
        thematic_filepath_to_edit = get_restore_path(yaml_config["thematic_file_to_edit"]["path"])
        if not os.path.isfile(thematic_filepath_to_edit):
            self.MsgBar.pushMessage(
                "Could not load the thematic layer '{}' ThRasE need this layer to setup the config, "
                "check the path in the yaml file".format(thematic_filepath_to_edit),
                level=Qgis.Critical)
            return
        if thematic_filepath_to_edit:
            load_and_select_filepath_in(self.QCBox_LayerToEdit, thematic_filepath_to_edit)
            self.select_layer_to_edit(self.QCBox_LayerToEdit.currentLayer())
            # band number
            if "band" in yaml_config["thematic_file_to_edit"]:
                self.QCBox_band_LayerToEdit.setCurrentIndex(yaml_config["thematic_file_to_edit"]["band"] - 1)
        # file config
        LayerToEdit.current.config_file = yaml_config["config_file"]
        # symbology and pixel table
        if "symbology" in yaml_config:
            LayerToEdit.current.symbology = yaml_config["symbology"]
        LayerToEdit.current.pixels = yaml_config["recode_pixel_table"]
        LayerToEdit.current.pixels_backup = yaml_config["recode_pixel_table_backup"]
        self.set_recode_pixel_table()
        self.update_recode_pixel_table()
        # view_widgets, active layers and edit tool
        if "active_layers_widget" in yaml_config:
            self.QPBtn_ActiveLayers.setChecked(yaml_config["active_layers_widget"])
            ViewWidget.active_layers_widget(enable=yaml_config["active_layers_widget"])
        if "edition_tools_widget" in yaml_config:
            self.QPBtn_EditionTools.setChecked(yaml_config["edition_tools_widget"])
            ViewWidget.edition_tools_widget(enable=yaml_config["edition_tools_widget"])
        for view_widget, yaml_view_widget in zip(ThRasEDialog.view_widgets, yaml_config["view_widgets"]):
            # active layers
            for active_layer, yaml_active_layer in zip(view_widget.active_layers, yaml_view_widget["active_layers"]):
                # TODO delete after some time, compatibility old yaml file
                if "layer" in yaml_active_layer:
                    yaml_active_layer["layer_path"] = yaml_active_layer["layer"]
                if "layer_name" not in yaml_active_layer:
                    if yaml_active_layer["layer_path"]:
                        yaml_active_layer["layer_name"] = os.path.splitext(os.path.basename(yaml_active_layer["layer_path"]))[0]
                    else:
                        yaml_active_layer["layer_name"] = ""

                # check if the file for this active layer if exists and is loaded in Qgis
                layer_name = yaml_active_layer["layer_name"]
                file_index = active_layer.QCBox_RenderFile.findText(layer_name, Qt.MatchFixedString)

                # select the layer or load it
                if file_index > 0:
                    # select layer if exists in Qgis
                    active_layer.QCBox_RenderFile.setCurrentIndex(file_index)
                    active_layer.set_render_layer(active_layer.QCBox_RenderFile.currentLayer())
                elif yaml_active_layer["layer_path"]:
                    layer_path = get_restore_path(yaml_active_layer["layer_path"])
                    if os.path.isfile(layer_path):
                        layer = load_and_select_filepath_in(active_layer.QCBox_RenderFile, layer_path, layer_name=layer_name)
                        active_layer.set_render_layer(layer)
                    else:
                        view_name = yaml_view_widget["view_name"] or view_widget.id
                        self.MsgBar.pushMessage(
                                "Could not load the layer '{layer_name}' in the view '{view_name}': "
                                "no such file {layer_path}".format(layer_name=layer_name, view_name=view_name,
                                                                   layer_path=layer_path),
                                level=Qgis.Warning, duration=5)
                        continue

                # opacity
                active_layer.layerOpacity.setValue(yaml_active_layer["opacity"])
                # on/off
                if not yaml_active_layer["is_active"]:
                    with block_signals_to(active_layer.OnOffActiveLayer):
                        active_layer.OnOffActiveLayer.setChecked(False)
                    active_layer.widget_ActiveLayer.setDisabled(True)
                    active_layer.disable()
            # edit tool
            if yaml_view_widget["mouse_pixel_value"]:
                view_widget.mousePixelValue2Table.setChecked(True)
            if yaml_view_widget["pixels_picker_enabled"]:
                view_widget.PixelsPicker.setChecked(True)
                view_widget.use_pixels_picker_for_edit()
            if yaml_view_widget["lines_picker_enabled"]:
                view_widget.LinesPicker.setChecked(True)
                view_widget.use_lines_picker_for_edit()
            if yaml_view_widget["line_buffer"]:
                selected_index = view_widget.LineBuffer.findText(yaml_view_widget["line_buffer"], Qt.MatchFixedString)
                view_widget.LineBuffer.setCurrentIndex(selected_index)
            if yaml_view_widget["lines_color"]:
                view_widget.change_lines_color(QColor(yaml_view_widget["lines_color"]))
            if yaml_view_widget["polygons_picker_enabled"]:
                view_widget.PolygonsPicker.setChecked(True)
                view_widget.use_polygons_picker_for_edit()
            if yaml_view_widget["polygons_color"]:
                view_widget.change_polygons_color(QColor(yaml_view_widget["polygons_color"]))
            if "freehand_picker_enabled" in yaml_view_widget and yaml_view_widget["freehand_picker_enabled"]:
                view_widget.FreehandPicker.setChecked(True)
                view_widget.use_freehand_picker_for_edit()
            if "freehand_color" in yaml_view_widget and yaml_view_widget["freehand_color"]:
                view_widget.change_freehand_color(QColor(yaml_view_widget["freehand_color"]))
        # set the render layers in the views
        if "number_active_layers" in yaml_config:
            # set the number of active layers
            self.QCBox_NoActiveLayers.setCurrentIndex(int(yaml_config["number_active_layers"]) - 1)
            self.set_active_layers(int(yaml_config["number_active_layers"]) - 1)
        else:
            # for old yaml files:
            # set the number of active layers based on the active layers in the views configured in the yaml file
            layer_in_row3 = any(al.id == 3 and al.layer is not None for al in view_widget.active_layers)
            layer_in_row2 = any(al.id == 2 and al.layer is not None for al in view_widget.active_layers)
            active_layers_index = 0 if not layer_in_row3 and not layer_in_row2 else 1 if not layer_in_row3 else 2
            self.QCBox_NoActiveLayers.setCurrentIndex(active_layers_index)
            self.set_active_layers(active_layers_index)

        # navigation
        if yaml_config["navigation"]["type"] != "free":
            # TODO delete after some time, compatibility old yaml file
            if yaml_config["navigation"]["type"] == "by tiles throughout the thematic file":
                yaml_config["navigation"]["type"] = "thematic file"
            if yaml_config["navigation"]["type"] == "by tiles throughout the AOI":
                yaml_config["navigation"]["type"] = "AOIs"
            if yaml_config["navigation"]["type"] == "by tiles throughout polygons":
                yaml_config["navigation"]["type"] = "polygons"
            if yaml_config["navigation"]["type"] == "by tiles throughout points":
                yaml_config["navigation"]["type"] = "points"
            if yaml_config["navigation"]["type"] == "by tiles throughout centroid of polygons":
                yaml_config["navigation"]["type"] = "centroid of polygons"
            if "build_tools" not in yaml_config["navigation"]:
                yaml_config["navigation"]["build_tools"] = True

            self.QPBtn_EnableNavigation.setChecked(True)
            selected_index = LayerToEdit.current.navigation_dialog.QCBox_BuildNavType.findText(
                yaml_config["navigation"]["type"], Qt.MatchFixedString)
            LayerToEdit.current.navigation_dialog.QCBox_BuildNavType.setCurrentIndex(selected_index)
            LayerToEdit.current.navigation_dialog.tileSize.setValue(yaml_config["navigation"]["tile_size"])
            if yaml_config["navigation"]["mode"] == "horizontal":
                LayerToEdit.current.navigation_dialog.nav_horizontal_mode.setChecked(True)
            else:
                LayerToEdit.current.navigation_dialog.nav_vertical_mode.setChecked(True)
            LayerToEdit.current.navigation_dialog.change_tiles_color(QColor(yaml_config["navigation"]["tiles_color"]))
            # recover some stuff by type navigation
            if yaml_config["navigation"]["type"] == "AOIs" and yaml_config["navigation"]["aois"]:
                # recover all aois and draw and save as rubber bands
                LayerToEdit.current.navigation_dialog.activate_deactivate_AOI_picker()
                nav_dialog_canvas = LayerToEdit.current.navigation_dialog.render_widget.canvas
                for feature in yaml_config["navigation"]["aois"]:
                    # rebuilt all features of the aois
                    for x, y in feature:
                        point = QgsPointXY(x, y)
                        nav_dialog_canvas.mapTool().rubber_band.addPoint(point)
                        nav_dialog_canvas.mapTool().aux_rubber_band.addPoint(point)
                    nav_dialog_canvas.mapTool().define_polygon()
                nav_dialog_canvas.mapTool().finish()
            if yaml_config["navigation"]["type"] in ["polygons",
                                                     "points",
                                                     "centroid of polygons"]:
                # recover the vector file
                load_and_select_filepath_in(LayerToEdit.current.navigation_dialog.QCBox_VectorFile,
                                            yaml_config["navigation"]["vector_file"])
            # build navigation with all settings loaded
            LayerToEdit.current.navigation_dialog.call_to_build_navigation()
            current_tile_id = yaml_config["navigation"]["current_tile_id"]
            LayerToEdit.current.navigation.current_tile = \
                next((tile for tile in LayerToEdit.current.navigation.tiles if tile.idx == current_tile_id), None)
            # navigation dialog
            LayerToEdit.current.navigation_dialog.QPBtn_BuildNavigationTools.setChecked(yaml_config["navigation"]["build_tools"])
            LayerToEdit.current.navigation_dialog.build_tools()
            if "size_dialog" in yaml_config["navigation"] and yaml_config["navigation"]:
                LayerToEdit.current.navigation_dialog.resize(*yaml_config["navigation"]["size_dialog"])
            if "extent_dialog" in yaml_config["navigation"] and yaml_config["navigation"]["extent_dialog"]:
                LayerToEdit.current.navigation_dialog.render_widget.canvas.setExtent(QgsRectangle(*yaml_config["navigation"]["extent_dialog"]))
            LayerToEdit.current.navigation_dialog.change_tile_from_slider(current_tile_id)
            LayerToEdit.current.navigation_dialog.change_tile_from_spinbox(current_tile_id)
            # navigation block widget
            self.currentTileKeepVisible.setChecked(yaml_config["navigation"]["tile_keep_visible"])
            self.enable_navigation_tool(True)
            self.NavigationBlockWidgetControls.setEnabled(True)
            self.QPBar_TilesNavigation.setMaximum(len(LayerToEdit.current.navigation.tiles))
            self.QPBar_TilesNavigation.setValue(current_tile_id)
        # restore the extent in the views using a view with a valid layer (not empty)
        if "extent" in yaml_config and yaml_config["extent"]:
            for view_widget in ThRasEDialog.view_widgets:
                if view_widget.is_active and not view_widget.render_widget.canvas.extent().isEmpty():
                    view_widget.render_widget.canvas.setExtent(QgsRectangle(*yaml_config["extent"]))
                    break

        # restore the CCD plugin widget config
        if "ccd_plugin_config" in yaml_config:
            if self.ccd_plugin_available:
                from CCD_Plugin.utils.config import restore_plugin_config
                # restore the CCD plugin config
                restore_plugin_config(self.ccd_plugin.id, yaml_config["ccd_plugin_config"])
                # set the CCD plugin widget visible
                self.QPBtn_CCDPlugin.setChecked(yaml_config["ccd_plugin_opened"])
                self.widget_ccd.setVisible(yaml_config["ccd_plugin_opened"])

    def keyPressEvent(self, event):
        # ignore esc key for close the main dialog
        if not event.key() == Qt.Key_Escape:
            super(ThRasEDialog, self).keyPressEvent(event)

    def closeEvent(self, event):
        # first prompt if dialog is opened
        if self.isVisible():
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
            self.tr("Select the thematic layer to edit"), "",
            self.tr("Raster files (*.tif *.img);;All files (*.*)"))
        if file_path != '' and os.path.isfile(file_path):
            # load to qgis and update combobox list
            layer = load_and_select_filepath_in(self.QCBox_LayerToEdit, file_path)
            self.select_layer_to_edit(layer)

    def enable_navigation_tool(self, checked):
        if not LayerToEdit.current:
            self.MsgBar.pushMessage("First select a valid thematic layer to edit", level=Qgis.Warning, duration=5)
            with block_signals_to(self.QPBtn_EnableNavigation):
                self.QPBtn_EnableNavigation.setChecked(False)
            return

        if checked:
            self.NavigationBlockWidget.setVisible(True)
            if LayerToEdit.current.navigation.is_valid:
                LayerToEdit.current.navigation.current_tile.show()
                if not self.currentTileKeepVisible.isChecked():
                    QTimer.singleShot(1200, LayerToEdit.current.navigation.current_tile.hide)
        else:
            self.NavigationBlockWidget.setHidden(True)
            if LayerToEdit.current.navigation.is_valid:
                LayerToEdit.current.navigation.current_tile.hide()

    @pyqtSlot()
    def go_to_current_tile(self):
        if LayerToEdit.current.navigation.is_valid:
            LayerToEdit.current.navigation.current_tile.focus()

    @pyqtSlot()
    def go_to_previous_tile(self):
        if LayerToEdit.current.navigation.is_valid:
            # set the previous tile
            LayerToEdit.current.navigation.set_current_tile(LayerToEdit.current.navigation.current_tile.idx - 1)
            # adjust navigation components
            self.QPBar_TilesNavigation.setValue(LayerToEdit.current.navigation.current_tile.idx)
            self.nextTile.setEnabled(True)
            if LayerToEdit.current.navigation.current_tile.idx == 1:  # first item
                self.previousTile.setEnabled(False)
            # adjust navigation components in navigation dialog
            with block_signals_to(LayerToEdit.current.navigation_dialog.currentTile):
                LayerToEdit.current.navigation_dialog.currentTile.setValue(LayerToEdit.current.navigation.current_tile.idx)
            with block_signals_to(LayerToEdit.current.navigation_dialog.SliderNavigation):
                LayerToEdit.current.navigation_dialog.SliderNavigation.setValue(LayerToEdit.current.navigation.current_tile.idx)
            LayerToEdit.current.navigation_dialog.highlight()

    @pyqtSlot()
    def go_to_next_tile(self):
        if LayerToEdit.current.navigation.is_valid:
            # set the next tile
            LayerToEdit.current.navigation.set_current_tile(LayerToEdit.current.navigation.current_tile.idx + 1)
            # adjust navigation components
            self.QPBar_TilesNavigation.setValue(LayerToEdit.current.navigation.current_tile.idx)
            self.previousTile.setEnabled(True)
            if LayerToEdit.current.navigation.current_tile.idx == len(LayerToEdit.current.navigation.tiles):  # last item
                self.nextTile.setEnabled(False)
            # adjust navigation components in navigation dialog
            with block_signals_to(LayerToEdit.current.navigation_dialog.currentTile):
                LayerToEdit.current.navigation_dialog.currentTile.setValue(LayerToEdit.current.navigation.current_tile.idx)
            with block_signals_to(LayerToEdit.current.navigation_dialog.SliderNavigation):
                LayerToEdit.current.navigation_dialog.SliderNavigation.setValue(LayerToEdit.current.navigation.current_tile.idx)
            LayerToEdit.current.navigation_dialog.highlight()

    @pyqtSlot()
    def current_tile_keep_visible(self):
        if self.currentTileKeepVisible.isChecked():
            LayerToEdit.current.navigation.current_tile.show()
        else:
            LayerToEdit.current.navigation.current_tile.hide()

        [view_widget.render_widget.refresh() for view_widget in ThRasEDialog.view_widgets if view_widget.is_active]

    @pyqtSlot()
    @error_handler
    def open_current_tile_navigation_in_google_engine(self):
        # create temp file
        from ThRasE.thrase import ThRasE
        if ThRasE.tmp_dir is None:
            ThRasE.tmp_dir = tempfile.mkdtemp()
        kml_file = tempfile.mktemp(
            prefix="TileNavigation_Num_{}_".format(LayerToEdit.current.navigation.current_tile.idx),
            suffix=".kml", dir=ThRasE.tmp_dir)
        # convert coordinates
        crsSrc = QgsCoordinateReferenceSystem(LayerToEdit.current.qgs_layer.crs())
        crsDest = QgsCoordinateReferenceSystem(4326)  # WGS84
        transform = QgsCoordinateTransform(crsSrc, crsDest, QgsProject.instance())
        # forward transformation: src -> dest
        polygon = transform.transformBoundingBox(LayerToEdit.current.navigation.current_tile.extent)
        xmin, ymin, xmax, ymax = polygon.toRectF().getCoords()

        # make file and save
        description = "Navigation tile number: <b>{Num}</b>/{total_tiles}<br/>" \
                      "Thematic editing: <em>{thematic_file}</em><br/>" \
                      "ThRasE Qgis-plugin".format(
                        Num=LayerToEdit.current.navigation.current_tile.idx,
                        total_tiles=len(LayerToEdit.current.navigation.tiles),
                        thematic_file=LayerToEdit.current.qgs_layer.name())
        kml_raw = """<?xml version="1.0" encoding="UTF-8"?>
            <kml xmlns="http://www.opengis.net/kml/2.2">
              <Document>
                <Style id="transBluePoly">
                  <LineStyle>
                    <width>1.5</width>
                  </LineStyle>
                  <PolyStyle>
                    <color>{kml_color}</color>
                  </PolyStyle>
                </Style>
                <Placemark>
                  <name>{name}</name>
                  <description>{desc}</description>
                  <styleUrl>#transBluePoly</styleUrl>
                  <Polygon>
                    <extrude>1</extrude>
                    <altitudeMode>relativeToGround</altitudeMode>
                    <outerBoundaryIs>
                      <LinearRing>
                        <coordinates>
                         {coord1},{alt}
                         {coord2},{alt}
                         {coord3},{alt}
                         {coord4},{alt}
                         {coord1},{alt}
                        </coordinates>
                      </LinearRing>
                    </outerBoundaryIs>
                  </Polygon>
                </Placemark>
              </Document>
            </kml>
            """.format(name="Tile Navigation Num {}".format(LayerToEdit.current.navigation.current_tile.idx),
                       desc=description, kml_color="00000000",
                       coord1="{},{}".format(xmin, ymin), coord2="{},{}".format(xmin, ymax),
                       coord3="{},{}".format(xmax, ymax), coord4="{},{}".format(xmax, ymin), alt=1000)
        outfile = open(kml_file, "w")
        outfile.writelines(kml_raw)
        outfile.close()

        open_file(kml_file)

    @pyqtSlot()
    def open_navigation_dialog(self):
        if LayerToEdit.current.navigation_dialog.isVisible():
            LayerToEdit.current.navigation_dialog.setWindowState(LayerToEdit.current.navigation_dialog.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
            LayerToEdit.current.navigation_dialog.raise_()
            LayerToEdit.current.navigation_dialog.activateWindow()
        else:
            LayerToEdit.current.navigation_dialog.show()

    def unset_thematic_layer_to_edit(self):
        # disable and clear the thematic file
        self.NavigationBlockWidget.setDisabled(True)
        self.QPBtn_ReloadRecodeTable.setDisabled(True)
        self.QPBtn_RestoreRecodeTable.setDisabled(True)
        self.QPBtn_AutoFill.setDisabled(True)
        self.Widget_GlobalEditingTools.setDisabled(True)
        self.QCBox_LayerToEdit.setCurrentIndex(-1)
        self.SaveConfig.setDisabled(True)
        with block_signals_to(self.QCBox_band_LayerToEdit):
            self.QCBox_band_LayerToEdit.clear()
        LayerToEdit.current = None
        [view_widget.widget_EditionTools.setEnabled(False) for view_widget in ThRasEDialog.view_widgets]

    def select_layer_to_edit(self, layer_selected):
        # first clear table
        self.recodePixelTable.setRowCount(0)
        self.recodePixelTable.setColumnCount(0)

        # first check
        if layer_selected is None:
            self.unset_thematic_layer_to_edit()
            return
        if not valid_file_selected_in(self.QCBox_LayerToEdit):
            self.MsgBar.pushMessage("The thematic layer to edit is not valid", level=Qgis.Warning, duration=5)
            self.unset_thematic_layer_to_edit()
            return
        # show warning for layer to edit different to tif format
        if layer_selected.source().split(".")[-1].lower() not in ["tif", "tiff"]:
            quit_msg = "Use raster files different to GTiff (tif or tiff) format has not been fully tested. " \
                       "GTiff files are recommended for editing.\n\n" \
                       "Do you want to continue anyway?"
            reply = QMessageBox.question(None, 'Thematic layer to edit in ThRasE',
                                         quit_msg, QMessageBox.Yes, QMessageBox.No)
            if reply == QMessageBox.No:
                self.unset_thematic_layer_to_edit()
                return

        # check if thematic layer to edit has data type as integer or byte
        if layer_selected.dataProvider().dataType(1) not in [1, 2, 3, 4, 5]:
            self.MsgBar.pushMessage("The thematic layer to edit must be byte or integer as data type",
                                    level=Qgis.Warning, duration=5)
            self.unset_thematic_layer_to_edit()
            return

        # set band count
        with block_signals_to(self.QCBox_band_LayerToEdit):
            self.QCBox_band_LayerToEdit.clear()
            self.QCBox_band_LayerToEdit.addItems([str(x) for x in range(1, layer_selected.bandCount() + 1)])

        self.setup_layer_to_edit()

    @error_handler
    def setup_layer_to_edit(self):
        layer = self.QCBox_LayerToEdit.currentLayer()
        band = int(self.QCBox_band_LayerToEdit.currentText())
        nodata = get_nodata_value(layer, band)

        # check if nodata is set, to handle it: unset or hide in recode table
        if nodata is not None:
            msgBox = QMessageBox()
            msgBox.setTextFormat(Qt.RichText)
            msgBox.setWindowTitle("ThRasE - How to handle the NoData value")
            msgBox.setText("The '{}' has {} as NoData. ThRasE cannot edit values assigned as NoData, "
                           "there are two possible options:".format(layer.name(), int(nodata)))
            msgBox.setInformativeText("<ol style='list-style-position: inside;'>"
                                      "<li style='margin-bottom: 8px;'>Unsets the NoData value for the thematic layer. The file will be "
                                      "modified, but you can edit the NoData value afterward</li>"
                                      "<li style='margin-bottom: 8px;'>Hides the NoData value in the recode table. The file will not be modified, "
                                      "but you will not be able to view or edit the NoData value</li></ol>")
            unset_button = msgBox.addButton("1. Unset NoData", QMessageBox.NoRole)
            hide_button = msgBox.addButton("2. Hide NoData", QMessageBox.NoRole)
            msgBox.setStandardButtons(QMessageBox.Cancel)
            msgBox.setDefaultButton(QMessageBox.Cancel)
            msgBox.exec()

            if msgBox.clickedButton() == unset_button:
                symbology_render = layer.renderer().clone()  # save the symbology before edit
                if unset_the_nodata_value(layer) == 0:
                    with block_signals_to(self.QCBox_LayerToEdit):
                        layer_name = layer.name()
                        layer_path = get_file_path_of_layer(layer)
                        self.QCBox_LayerToEdit.setCurrentIndex(-1)
                        unload_layer(layer_path)
                        layer = load_layer(layer_path, name=layer_name)
                        # add nodata value to symbology as black color
                        new_symbology_render = add_color_value_to_symbology(symbology_render, nodata, "black")
                        if new_symbology_render:
                            # restore symbology with the new nodata value
                            layer.setRenderer(new_symbology_render)
                        layer.triggerRepaint()
                        layer.reload()
                        # select the sampling file in combobox
                        selected_index = self.QCBox_LayerToEdit.findText(layer.name(), Qt.MatchFixedString)
                        self.QCBox_LayerToEdit.setCurrentIndex(selected_index)
                    with block_signals_to(self.QCBox_band_LayerToEdit):
                        band_idx = self.QCBox_band_LayerToEdit.findText(str(band), Qt.MatchFixedString)
                        self.QCBox_band_LayerToEdit.setCurrentIndex(band_idx)
                    nodata = None
                    self.MsgBar.pushMessage(
                        "The NoData value for the thematic layer '{}' was successfully unset".format(layer.name()),
                        level=Qgis.Success, duration=5)
                else:
                    self.MsgBar.pushMessage(
                        "It was not possible to unset the NoData value for the thematic layer '{}'".format(layer.name()),
                        level=Qgis.Critical, duration=5)
                    return
            elif msgBox.clickedButton() != hide_button:
                # cancel action
                self.unset_thematic_layer_to_edit()
                return

        if (layer.id(), band) in LayerToEdit.instances:
            layer_to_edit = LayerToEdit.instances[(layer.id(), band)]
        else:
            # create new instance
            layer_to_edit = LayerToEdit(layer, band)
            # init data for recode pixel table
            recode_pixel_table_status = layer_to_edit.setup_pixel_table(nodata=nodata)
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
        # tooltip
        self.QCBox_LayerToEdit.setToolTip(layer_to_edit.qgs_layer.name())
        # enable some components
        self.NavigationBlockWidget.setEnabled(True)
        [view_widget.widget_EditionTools.setEnabled(True) for view_widget in ThRasEDialog.view_widgets]
        self.QPBtn_ReloadRecodeTable.setEnabled(True)
        self.QPBtn_RestoreRecodeTable.setEnabled(True)
        self.QPBtn_AutoFill.setEnabled(True)
        self.Widget_GlobalEditingTools.setEnabled(True)
        self.SaveConfig.setEnabled(True)

    @pyqtSlot()
    @error_handler
    def update_recode_pixel_table(self):
        layer_to_edit = LayerToEdit.current
        layer_to_edit.old_new_value = {}
        if not layer_to_edit or layer_to_edit.pixels is None:
            return

        for row_idx, pixel in enumerate(layer_to_edit.pixels):
            # assign the new value
            new_value = self.recodePixelTable.item(row_idx, 3).text()
            try:
                if new_value == "":
                    pixel["new_value"] = None
                elif float(new_value) == int(new_value) and int(new_value) != int(self.recodePixelTable.item(row_idx, 2).text()):
                    pixel["new_value"] = int(new_value)
                    layer_to_edit.old_new_value[pixel["value"]] = pixel["new_value"]
            except:
                pass
            # assign the on state
            on = self.recodePixelTable.item(row_idx, 1)
            if on.checkState() == 2:
                pixel["s/h"] = True
            if on.checkState() == 0:
                pixel["s/h"] = False

        # update pixel class visibility
        pixel_class_visibility = [255 if self.recodePixelTable.item(row_idx, 1).checkState() == 2 else 0
                                  for row_idx in range(len(layer_to_edit.pixels))]
        layer_to_edit.symbology = [(row[0], row[1], (row[2][0], row[2][1], row[2][2], pcv))
                                   for row, pcv in zip(layer_to_edit.symbology, pixel_class_visibility)]
        apply_symbology(layer_to_edit.qgs_layer, layer_to_edit.band, layer_to_edit.symbology)

        # update table
        self.set_recode_pixel_table()

        # update classes to edit label
        number_classes_to_edit = sum([1 if self.recodePixelTable.item(idx, 3).text() != "" else 0
                                      for idx in range(len(layer_to_edit.pixels))])
        self.QLbl_NumberClassesToEdit.setText("({} {} to edit)".format(number_classes_to_edit,
                                                                       "class" if number_classes_to_edit == 1 else "classes"))

    @error_handler
    def set_recode_pixel_table(self):
        layer_to_edit = LayerToEdit.current

        if not layer_to_edit or layer_to_edit.pixels is None:
            # clear table
            self.recodePixelTable.clear()
            return

        with block_signals_to(self.recodePixelTable):
            header = ["", "", "Curr Value", "New Value", ""]
            row_length = len(layer_to_edit.pixels)
            # init table
            self.recodePixelTable.setRowCount(row_length)
            self.recodePixelTable.setColumnCount(5)
            self.recodePixelTable.horizontalHeader().setMinimumSectionSize(45)
            # hidden row labels
            self.recodePixelTable.verticalHeader().setVisible(False)
            # add Header
            self.recodePixelTable.setHorizontalHeaderLabels(header)
            # insert items
            for col_idx, header in enumerate(header):
                if col_idx == 0:  # color class
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem()
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setToolTip("Class color, click to edit.\n"
                                              "INFO: editing the color is only temporary and does not affect the layer")
                        item_table.setBackground(QColor(pixel["color"]["R"], pixel["color"]["G"],
                                                        pixel["color"]["B"], pixel["color"]["A"]))
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)
                if col_idx == 1:  # Show/Hide
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem()
                        item_table.setFlags(item_table.flags() | Qt.ItemIsUserCheckable)
                        item_table.setFlags(item_table.flags() | Qt.ItemIsEnabled)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        item_table.setToolTip("Show/Hide the pixel class value.\n"
                                              "INFO: It is only temporary and does not affect the layer.\n"
                                              "WARNING: If the class is hidden it does not avoid being edited!")
                        if pixel["s/h"]:
                            item_table.setCheckState(Qt.Checked)
                        else:
                            item_table.setCheckState(Qt.Unchecked)
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)
                if col_idx == 2:  # Curr Value
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
                if col_idx == 3:  # New Value
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem(str(pixel["new_value"]) if pixel["new_value"] is not None else "")
                        item_table.setFlags(item_table.flags() | Qt.ItemIsEnabled | Qt.ItemIsEditable)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        item_table.setToolTip("Set the new value for this class.\n"
                                              "WARNING: After each editing operation, the layer is saved on disk!")
                        if pixel["new_value"] is not None and pixel["new_value"] != pixel["value"]:
                            font = QFont()
                            font.setBold(True)
                            item_table.setFont(font)
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)
                if col_idx == 4:  # clear new value
                    for row_idx, pixel in enumerate(layer_to_edit.pixels):
                        item_table = QTableWidgetItem()
                        path = ':/plugins/thrase/icons/clear.svg'
                        icon = QIcon(path)
                        item_table.setIcon(icon)
                        item_table.setFlags(item_table.flags() & ~Qt.ItemIsSelectable)
                        item_table.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                        item_table.setToolTip("Clear this row")
                        self.recodePixelTable.setItem(row_idx, col_idx, item_table)

            # adjust size of Table
            self.recodePixelTable.horizontalHeader().setMinimumSectionSize(10)
            self.recodePixelTable.resizeColumnsToContents()
            self.recodePixelTable.resizeRowsToContents()
            self.recodePixelTable.setColumnWidth(0, 45)
            # adjust the editor block based on table content
            table_width = self.recodePixelTable.horizontalHeader().length() + 40
            self.EditionBlock.setMaximumWidth(table_width)
            self.EditionBlock.setMinimumWidth(table_width)

    @pyqtSlot(QTableWidgetItem)
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
        # clear the current new value for the row clicked
        elif table_item.column() == 4:
            self.recodePixelTable.item(table_item.row(), 3).setText("")


    @pyqtSlot()
    @error_handler
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
            self.Widget_GlobalEditingTools.setEnabled(False)
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
        # restore the opacity of all active layers
        [al.update_layer_opacity() for als in [view_widget.active_layers for view_widget in ThRasEDialog.view_widgets]
         for al in als]

    @pyqtSlot()
    @error_handler
    def restore_recode_table(self):
        # restore the pixels and symbology variables
        LayerToEdit.current.pixels = deepcopy(LayerToEdit.current.pixels_backup)
        LayerToEdit.current.setup_symbology()
        # restore table
        self.set_recode_pixel_table()
        # update pixel class visibility
        apply_symbology(LayerToEdit.current.qgs_layer, LayerToEdit.current.band, LayerToEdit.current.symbology)
        # restore the opacity of all active layers
        [al.update_layer_opacity() for als in [view_widget.active_layers for view_widget in ThRasEDialog.view_widgets]
         for al in als]
        # enable some components
        self.NavigationBlockWidget.setEnabled(True)
        [view_widget.widget_EditionTools.setEnabled(True) for view_widget in ThRasEDialog.view_widgets]
        self.Widget_GlobalEditingTools.setEnabled(True)

    @pyqtSlot()
    def open_autofill_dialog(self):
        if self.autofill_dialog.isVisible():
            self.autofill_dialog.setWindowState(self.autofill_dialog.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
            self.autofill_dialog.raise_()
            self.autofill_dialog.activateWindow()
        else:
            self.autofill_dialog.show()

    @pyqtSlot()
    def apply_whole_image(self):
        # first prompt
        quit_msg = "This action applies the changes defined in the pixel recoding table to the entire image. " \
                   "This operation cannot be undone. \n\n" \
                   "Target file: \"{}\"".format(LayerToEdit.current.file_path)
        reply = QMessageBox.question(None, 'Applying changes to the whole image',
                                     quit_msg, QMessageBox.Apply | QMessageBox.Cancel, QMessageBox.Cancel)
        if reply == QMessageBox.Apply:
            if LayerToEdit.current.edit_whole_image() is not False:
                self.MsgBar.pushMessage(
                    "Changes in recode pixels table were successfully applied to the whole thematic file",
                    level=Qgis.Success, duration=5)

    @pyqtSlot()
    def apply_from_thematic_classes_dialog(self):
        # check if the recode pixel table is empty
        if not LayerToEdit.current.old_new_value:
            self.MsgBar.pushMessage(
                "There are no changes to apply in the recode pixel table. Please set new pixel values first",
                level=Qgis.Warning, duration=5)
            return

        self.apply_from_thematic_classes.setup_gui()
        if self.apply_from_thematic_classes.exec_():
            self.MsgBar.pushMessage(
                "Changes in recode pixels table were successfully applied using thematic file classes",
                level=Qgis.Success, duration=5)

    @pyqtSlot()
    def file_dialog_save_thrase_config(self):
        if LayerToEdit.current.config_file:
            suggested_filename = LayerToEdit.current.config_file
        else:
            path, filename = os.path.split(LayerToEdit.current.file_path)
            suggested_filename = os.path.splitext(os.path.join(path, filename))[0] + "_thrase.yaml"

        output_file, _ = QFileDialog.getSaveFileName(self, self.tr("Save the current configuration of the ThRasE plugin"),
                                                  suggested_filename,
                                                  self.tr("YAML files (*.yaml *.yml);;All files (*.*)"))

        if output_file is None or output_file == '':
            return

        if not output_file.endswith(('.yaml', '.yml')):
            output_file += ".yaml"
        
        LayerToEdit.current.save_config(output_file)
        self.MsgBar.pushMessage("ThRasE", "Configuration file saved successfully in '{}'".format(output_file),
                                level=Qgis.Success, duration=5)

    @pyqtSlot(bool)
    def ccd_plugin_widget(self, checked):
        if checked:
            self.widget_ccd.setVisible(True)
        else:
            self.widget_ccd.setVisible(False)
            if self.ccd_plugin_available:
                from CCD_Plugin.gui.CCD_Plugin_dockwidget import PickerCoordsOnMap
                PickerCoordsOnMap.delete_markers()

FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'init_dialog.ui'))


class InitDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self):
        QtWidgets.QDialog.__init__(self)
        self.setupUi(self)

