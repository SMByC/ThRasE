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
import functools
import traceback
import os, sys, subprocess
from collections import OrderedDict
try:
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeLoader

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication, QMessageBox, QPushButton
from qgis.PyQt.QtGui import QCursor
from qgis.core import Qgis
from qgis.utils import iface


def error_handler(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as err:
            # restore mouse
            QApplication.restoreOverrideCursor()
            QApplication.processEvents()

            # select the message bar
            from ThRasE.thrase import ThRasE
            if ThRasE.dialog:
                msg_bar = ThRasE.dialog.MsgBar
            else:
                msg_bar = iface.messageBar()

            msg_bar.clearWidgets()

            # message in status bar with details
            def details_message_box(error, more_details):
                msgBox = QMessageBox()
                msgBox.setWindowTitle("ThRasE - Error handler")
                msgBox.setText("<i>{}</i>".format(error))
                msgBox.setInformativeText("If you consider this as an error of ThRasE, report it in "
                                          "<a href='https://github.com/SMByC/ThRasE/issues'>issue tracker</a>")
                msgBox.setDetailedText(more_details)
                msgBox.setTextFormat(Qt.RichText)
                msgBox.setStandardButtons(QMessageBox.Ok)
                msgBox.exec()
                del msgBox

            msg_error = "Ups! an error has occurred in ThRasE plugin"
            widget = msg_bar.createMessage("ThRasE", msg_error)
            error = err
            more_details = traceback.format_exc()

            button = QPushButton(widget)
            button.setText("Show details...")
            button.pressed.connect(lambda: details_message_box(error, more_details))
            widget.layout().addWidget(button)

            msg_bar.pushWidget(widget, level=Qgis.Warning, duration=20)

    return wrapper


def wait_process(func):
    @error_handler
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # mouse wait
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        # do
        obj_returned = func(*args, **kwargs)
        # restore mouse
        QApplication.restoreOverrideCursor()
        QApplication.processEvents()
        # finally return the object by f
        return obj_returned
    return wrapper


def open_file(filename):
    """Open a file with the standard application"""
    filename = os.path.abspath(filename)

    if sys.platform == "linux" or sys.platform == "linux2":
        # Linux
        subprocess.call(["xdg-open", filename])
    elif sys.platform == "darwin":
        # OS X
        subprocess.call(["open", filename])
    elif sys.platform == "win32":
        # Windows
        os.startfile(filename)


class block_signals_to(object):
    """Block all signals emits from specific QT object"""
    def __init__(self, object_to_block):
        self.object_to_block = object_to_block

    def __enter__(self):
        # block
        try:
            self.object_to_block.blockSignals(True)
        except RuntimeError:
            pass  # Object has been deleted

    def __exit__(self, type, value, traceback):
        # unblock
        try:
            self.object_to_block.blockSignals(False)
        except RuntimeError:
            pass  # Object has been deleted


# --------------------------------------------------------------------------
# Legacy YAML loader support

class LegacyLoader(SafeLoader):
    """Custom YAML loader for handling legacy configuration files."""
    pass

def _normalize_pairs(items):
    """Normalize a sequence or dict into a list of (key, value) tuples."""
    normalized = []
    if isinstance(items, dict):
        items = items.items()
    for item in items:
        if isinstance(item, dict) and item:
            key, value = next(iter(item.items()))
            normalized.append((key, value))
        elif isinstance(item, (list, tuple)) and item:
            key = item[0]
            value = item[1] if len(item) > 1 else None
            normalized.append((key, value))
        elif item is not None:
            normalized.append((item, None))
    return normalized

def construct_python_tuple(loader, node):
    """Construct a Python tuple from a YAML sequence node."""
    return tuple(loader.construct_sequence(node, deep=True))

def construct_ordered_dict(loader, node):
    """Construct an OrderedDict from a YAML sequence node, supporting legacy formats."""
    params = loader.construct_sequence(node, deep=True)
    kwargs = {}
    if params and isinstance(params[-1], dict):
        kwargs = params.pop() if params[-1] else {}
    items = params[0] if len(params) == 1 and isinstance(params[0], (list, tuple)) else params
    if isinstance(items, dict):
        items = items.items()
    return OrderedDict(_normalize_pairs(items), **kwargs)

def construct_yaml_map(loader, node):
    """Construct an OrderedDict from a YAML map node."""
    loader.flatten_mapping(node)
    pairs = loader.construct_pairs(node, deep=True)
    return OrderedDict(pairs)

def construct_yaml_omap(loader, node):
    """Construct an OrderedDict from a YAML omap node."""
    items = loader.construct_sequence(node, deep=True)
    return OrderedDict(_normalize_pairs(items))

# Register constructors
LegacyLoader.add_constructor('tag:yaml.org,2002:python/tuple', construct_python_tuple)
LegacyLoader.add_constructor('tag:yaml.org,2002:python/object/apply:collections.OrderedDict', construct_ordered_dict)
LegacyLoader.add_constructor('tag:yaml.org,2002:map', construct_yaml_map)
LegacyLoader.add_constructor('tag:yaml.org,2002:omap', construct_yaml_omap)

# --------------------------------------------------------------------------
