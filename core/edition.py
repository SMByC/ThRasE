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

from qgis.core import QgsRaster, QgsPointXY

from ThRasE.core.navigation import Navigation
from ThRasE.utils.others_utils import get_xml_style
from ThRasE.utils.qgis_utils import get_file_path_of_layer


class LayerToEdit(object):
    instances = {}
    current = None

    def __init__(self, layer, band):
        self.qgs_layer = layer
        self.file_path = get_file_path_of_layer(layer)
        self.band = band
        self.navigation = Navigation()
        # store pixels: value, color, new_value, on/off
        #  -> [{"value", "color": {"R", "G", "B", "A"}, "new_value": int, "on": bool}, ...]
        self.pixels = None

        self.setup_pixel_table()

        LayerToEdit.instances[(layer.id(), band)] = self

    def extent(self):
        return self.qgs_layer.extent()

    def get_pixel_value_from_xy(self, x, y):
        return self.qgs_layer.dataProvider().identify(QgsPointXY(x, y), QgsRaster.IdentifyFormatValue).results()[self.band]

    def get_pixel_value_from_pnt(self, point):
        return self.qgs_layer.dataProvider().identify(point, QgsRaster.IdentifyFormatValue).results()[self.band]

    def setup_pixel_table(self):
        if self.pixels is None:
            xml_style_items = get_xml_style(self.qgs_layer, self.band)
            if xml_style_items is None:
                self.pixels = None
                return

            self.pixels = []
            for xml_item in xml_style_items:
                pixel = {"value": int(xml_item.get("value")), "color": {}, "new_value": None, "on": True}

                item_color = xml_item.get("color").lstrip('#')
                item_color = tuple(int(item_color[i:i + 2], 16) for i in (0, 2, 4))
                pixel["color"]["R"] = item_color[0]
                pixel["color"]["G"] = item_color[1]
                pixel["color"]["B"] = item_color[2]
                pixel["color"]["A"] = int(xml_item.get("alpha"))

                self.pixels.append(pixel)

