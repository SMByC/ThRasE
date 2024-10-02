# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ThRasE
 
 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2024 by Xavier Corredor Llano, SMByC
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

import os.path
import shutil
from pathlib import Path

from qgis.PyQt.QtCore import QSettings, QTranslator, qVersion, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.utils import iface

# Initialize Qt resources from file resources.py
from .resources import *

from ThRasE.gui.main_dialog import ThRasEDialog
from ThRasE.gui.about_dialog import AboutDialog
from ThRasE.utils.qgis_utils import unload_layer


class ThRasE:
    """QGIS Plugin Implementation."""
    dialog = None
    # tmp dir for all process and intermediate files, only on demand
    tmp_dir = None

    def __init__(self, iface):
        """Constructor.

        :param iface: An interface instance that will be passed to this class
            which provides the hook by which you can manipulate the QGIS
            application at run time.
        :type iface: QgsInterface
        """
        # Save reference to the QGIS interface
        self.iface = iface
        # initialize plugin directory
        self.plugin_dir = os.path.dirname(__file__)
        # initialize locale
        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(self.plugin_dir, 'i18n', 'ThRasE_{}.qm'.format(locale))

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)

            if qVersion() > '4.3.3':
                QCoreApplication.installTranslator(self.translator)

        self.menu_name_plugin = self.tr("ThRasE - Thematic Raster Editor")
        self.pluginIsActive = False
        ThRasE.dialog = None

        self.about_dialog = AboutDialog()

    # noinspection PyMethodMayBeStatic
    def tr(self, message):
        """Get the translation for a string using Qt translation API.

        We implement this ourselves since we do not inherit QObject.

        :param message: String for translation.
        :type message: str, QString

        :returns: Translated version of message.
        :rtype: QString
        """
        # noinspection PyTypeChecker,PyArgumentList,PyCallByClass
        return QCoreApplication.translate('ThRasE', message)

    def initGui(self):
        ### Main dialog menu
        # Create action that will start plugin configuration
        icon_path = ':/plugins/thrase/icons/thrase.svg'
        self.dockable_action = QAction(QIcon(icon_path), "ThRasE", self.iface.mainWindow())
        # connect the action to the run method
        self.dockable_action.triggered.connect(self.run)
        # Add toolbar button and menu item
        self.iface.addToolBarIcon(self.dockable_action)
        self.iface.addPluginToMenu(self.menu_name_plugin, self.dockable_action)

        # Plugin info
        # Create action that will start plugin configuration
        icon_path = ':/plugins/thrase/icons/about.svg'
        self.about_action = QAction(QIcon(icon_path), self.tr('About'), self.iface.mainWindow())
        # connect the action to the run method
        self.about_action.triggered.connect(self.about)
        # Add toolbar button and menu item
        self.iface.addPluginToMenu(self.menu_name_plugin, self.about_action)

    def about(self):
        self.about_dialog.show()

    #--------------------------------------------------------------------------

    def run(self):
        """Run method that loads and starts the plugin"""

        if not self.pluginIsActive:
            self.pluginIsActive = True

            # dialog may not exist if:
            #    first run of plugin
            #    removed on close (see self.onClosePlugin method)
            if ThRasE.dialog is None:
                ThRasE.dialog = ThRasEDialog()

            # connect to provide cleanup on closing of dialog
            ThRasE.dialog.closingPlugin.connect(self.onClosePlugin)

            # setup and show the dialog
            if not ThRasE.dialog.setup_gui():
                return
            ThRasE.dialog.show()
            # Run the dialog event loop
            result = ThRasE.dialog.exec_()
            # See if OK was pressed
            if result:
                # Do something useful here - delete the line containing pass and
                # substitute with your code.
                pass
        else:
            # an instance of ThRasE is already created
            # brings that instance to front even if it is minimized
            ThRasE.dialog.setWindowState(ThRasE.dialog.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
            ThRasE.dialog.raise_()
            ThRasE.dialog.activateWindow()

    #--------------------------------------------------------------------------

    def onClosePlugin(self):
        """Cleanup necessary items here when plugin is closed"""
        from ThRasE.core.edition import LayerToEdit

        # restore the recode pixel table to original (if was changed) of the thematic raster to edit
        if LayerToEdit.current:
            ThRasE.dialog.restore_recode_table()

        # restore the opacity of all active layers to 100%
        [al.update_layer_opacity(100) for als in [view_widget.active_layers for view_widget in ThRasEDialog.view_widgets]
         for al in als if al.opacity < 100]

        # close the navigation dialog if is open
        if LayerToEdit.current and LayerToEdit.current.navigation_dialog and LayerToEdit.current.navigation_dialog.isVisible():
            LayerToEdit.current.navigation_dialog.close()
            LayerToEdit.current.navigation_dialog = None

        # close the autofill dialog if is open
        if hasattr(ThRasE.dialog, "autofill_dialog") and ThRasE.dialog.autofill_dialog and ThRasE.dialog.autofill_dialog.isVisible():
            ThRasE.dialog.autofill_dialog.close()
            ThRasE.dialog.autofill_dialog = None

        self.removes_temporary_files()

        # remove this statement if dialog is to remain
        # for reuse if plugin is reopened
        # Commented next statement since it causes QGIS crashe
        # when closing the docked window:
        ThRasE.dialog.close()
        ThRasE.dialog = None

        # reset some variables
        self.pluginIsActive = False
        ThRasEDialog.view_widgets = []
        LayerToEdit.instances = {}
        LayerToEdit.current = None

        from qgis.utils import reloadPlugin
        reloadPlugin("ThRasE - Thematic Raster Editor")

    def unload(self):
        """Removes the plugin menu item and icon from QGIS GUI."""
        self.removes_temporary_files()
        # Remove the plugin menu item and icon
        self.iface.removePluginMenu(self.menu_name_plugin, self.dockable_action)
        self.iface.removePluginMenu(self.menu_name_plugin, self.about_action)
        self.iface.removeToolBarIcon(self.dockable_action)

        if ThRasE.dialog:
            ThRasE.dialog.close()

    @staticmethod
    def removes_temporary_files():
        if not ThRasE.dialog:
            return
        # unload all layers instances from Qgis saved in tmp dir
        try:
            d = ThRasE.tmp_dir
            files_in_tmp_dir = [Path(d, f) for f in os.listdir(d)
                                if Path(d, f).is_file()]
        except: files_in_tmp_dir = []

        for file_tmp in files_in_tmp_dir:
            unload_layer(str(file_tmp))

        # clear ThRasE.tmp_dir
        if ThRasE.tmp_dir and os.path.isdir(ThRasE.tmp_dir):
            shutil.rmtree(ThRasE.tmp_dir, ignore_errors=True)
        ThRasE.tmp_dir = None

        # clear qgis main canvas
        iface.mapCanvas().refreshAllLayers()
