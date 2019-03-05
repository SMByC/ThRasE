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
from qgis.core import Qgis, QgsUnitTypes


class Navigation(object):
    def __init__(self):
        pass

    def set_units(self):
        # set/update the units in tileSize item
        layer_dist_unit = layer_selected.crs().mapUnits()
        str_unit = QgsUnitTypes.toString(layer_dist_unit)
        abbr_unit = QgsUnitTypes.toAbbreviatedString(layer_dist_unit)
        # Set the properties of the QdoubleSpinBox based on the QgsUnitTypes of the thematic layer
        # https://qgis.org/api/classQgsUnitTypes.html
        self.tileSize.setSuffix(" {}".format(abbr_unit))
        self.tileSize.setToolTip(
            "The height/width for the tile size in {} (units based on layer to edit selected)".format(str_unit))
        self.tileSize.setRange(0, 360 if layer_dist_unit == QgsUnitTypes.DistanceDegrees else 10e6)
        self.tileSize.setDecimals(
            4 if layer_dist_unit in [QgsUnitTypes.DistanceKilometers, QgsUnitTypes.DistanceNauticalMiles,
                                     QgsUnitTypes.DistanceMiles, QgsUnitTypes.DistanceDegrees] else 1)
        self.tileSize.setSingleStep(
            0.0001 if layer_dist_unit in [QgsUnitTypes.DistanceKilometers, QgsUnitTypes.DistanceNauticalMiles,
                                          QgsUnitTypes.DistanceMiles, QgsUnitTypes.DistanceDegrees] else 1)
        default_tile_size = {QgsUnitTypes.DistanceMeters: 120, QgsUnitTypes.DistanceKilometers: 0.120,
                             QgsUnitTypes.DistanceFeet: 393, QgsUnitTypes.DistanceNauticalMiles: 0.065,
                             QgsUnitTypes.DistanceYards: 132, QgsUnitTypes.DistanceMiles: 0.075,
                             QgsUnitTypes.DistanceDegrees: 0.0011, QgsUnitTypes.DistanceCentimeters: 12000,
                             QgsUnitTypes.DistanceMillimeters: 120000}
        self.tileSize.setValue(default_tile_size[layer_dist_unit])



