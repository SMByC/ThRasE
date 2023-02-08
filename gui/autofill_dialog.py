# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ThRasE

 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2023 by Xavier Corredor Llano, SMByC
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

from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog
from qgis.core import Qgis

# plugin path
plugin_folder = os.path.dirname(os.path.dirname(__file__))
FORM_CLASS, _ = uic.loadUiType(Path(plugin_folder, 'ui', 'autofill_dialog.ui'))


class AutoFill(QDialog, FORM_CLASS):
    instance = None

    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.setupUi(self)
        self.setup_gui()
        AutoFill.instance = self

    def setup_gui(self):
        # adjust the column width of the table
        self.AutoFillTable.setColumnWidth(0, 260)
        self.AutoFillTable.setColumnWidth(1, 140)
        # adjust the width of the dialog
        self.resize(460, 650)

        self.QPBtn_ApplyAutoFill.clicked.connect(self.apply_autofill)

    def check_condition(self, condition):
        if condition == "*":
            return True
        if condition is None or condition == '':
            return False
        try:
            condition_to_evaluate = condition.replace('c', 'C').replace('C', '1')
            eval(condition_to_evaluate)
            return True
        except Exception:
            self.MsgBar.pushMessage(condition.replace('<', '＜').replace('>', '＞'), "Invalid condition", level=Qgis.Warning, duration=5)
            return False

    def check_value(self, value):
        if value is None or value == '':
            return True
        try:
            value_to_evaluate = value.replace('c', 'C').replace('C', '1')
            eval(value_to_evaluate)
            return True
        except Exception:
            self.MsgBar.pushMessage(value, "Invalid value", level=Qgis.Warning, duration=5)
            return False

    def apply_autofill(self):
        from ThRasE.thrase import ThRasE
        from ThRasE.core.edition import LayerToEdit

        # first close active items opened in the table
        self.AutoFillTable.setCurrentItem(None)

        # go through the table to get the condition and value
        autofill_entries = []
        for row in range(self.AutoFillTable.rowCount()):
            condition = self.AutoFillTable.item(row, 0)
            condition = condition.text().strip() if condition else None
            value = self.AutoFillTable.item(row, 1)
            value = value.text().strip() if value else None

            if self.check_condition(condition) and self.check_value(value):
                autofill_entries.append((condition, value))

        if not autofill_entries:
            return

        curr_values = [ThRasE.dialog.recodePixelTable.item(row, 2).text()
                       for row in range(ThRasE.dialog.recodePixelTable.rowCount())]
        new_values = [""]*len(curr_values)

        # apply the autofill
        for condition, value in autofill_entries:
            condition = condition.replace('c', 'C').strip()
            value = value.replace('c', 'C').strip()

            for idx_row, curr_value in enumerate(curr_values):
                condition_to_evaluate = condition.replace('C', curr_value)

                if condition_to_evaluate == '*' or eval(condition_to_evaluate):
                    new_values[idx_row] = eval(value.replace('C', curr_value)) if value != "" else None

        # update the values in the table
        for idx_row, new_value in enumerate(new_values):
            LayerToEdit.current.pixels[idx_row]["new_value"] = int(round(float(new_value))) if new_value not in [None, ""] else None

        ThRasE.dialog.set_recode_pixel_table()
        ThRasE.dialog.update_recode_pixel_table()

