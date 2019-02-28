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
from ThRasE.utils.qgis_utils import get_file_path_of_layer


class LayerToEdit(object):
    instances = {}

    def __init__(self, layer, band):
        self.qgs_layer = layer
        self.file_path = get_file_path_of_layer(layer)
        self.band = band
        self.navigation = Navigation()

        LayerToEdit.instances[(layer.id(), band)] = self

    def extent(self):
        return self.qgs_layer.extent()

    def get_pixel_value_from_xy(self, x, y):
        return self.qgs_layer.dataProvider().identify(QgsPointXY(x, y), QgsRaster.IdentifyFormatValue).results()[self.band]

    def get_pixel_value_from_pnt(self, point):
        return self.qgs_layer.dataProvider().identify(point, QgsRaster.IdentifyFormatValue).results()[self.band]

    def setup_pixel_table(self):
        return



