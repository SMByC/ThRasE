# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ThRasE
 
 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2022 by Xavier Corredor Llano, SMByC
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
import numpy as np
import multiprocessing
import xml.etree.ElementTree as ET
from random import randrange
from osgeo import gdal

from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import QgsPalettedRasterRenderer

from ThRasE.utils.qgis_utils import get_file_path_of_layer
from ThRasE.utils.system_utils import wait_process


def mask(input_list, boolean_mask):
    """Apply boolean mask to input list

    Args:
        input_list (list): Input list for apply mask
        boolean_mask (list): The boolean mask list

    Examples:
        >>> mask(['A','B','C','D'], [1,0,1,0])
        ['A', 'C']
    """
    return [i for i, b in zip(input_list, boolean_mask) if b]

# --------------------------------------------------------------------------


@wait_process
def auto_symbology_classification_render(layer, band):
    # get the unique values in the band
    rows = layer.height()
    cols = layer.width()
    provider = layer.dataProvider()
    bl = provider.block(band, provider.extent(), cols, rows)
    unique_values = list(set([bl.value(r, c) for r in range(rows) for c in range(cols)]))

    # fill categories
    categories = []
    for unique_value in unique_values:
        categories.append(QgsPalettedRasterRenderer.Class(
            unique_value, QColor(randrange(0, 256), randrange(0, 256), randrange(0, 256)), str(unique_value)))

    renderer = QgsPalettedRasterRenderer(layer.dataProvider(), band, categories)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def get_xml_style(layer, band):
    current_style = layer.styleManager().currentStyle()
    layer_style = layer.styleManager().style(current_style)
    xml_style_str = layer_style.xmlData()
    xml_style = ET.fromstring(xml_style_str)

    # for singleband_pseudocolor
    xml_style_items = xml_style.findall('pipe/rasterrenderer[@band="{}"]/rastershader/colorrampshader/item'.format(band))
    if not xml_style_items:
        # for unique values
        xml_style_items = xml_style.findall('pipe/rasterrenderer[@band="{}"]/colorPalette/paletteEntry'.format(band))

    check_int_values = [int(float(xml_item.get("value"))) == float(xml_item.get("value")) for xml_item in xml_style_items]

    if not xml_style_items or False in check_int_values:
        msg = "The selected layer \"{layer}\"{band} doesn't have an appropriate symbology for ThRasE, " \
              "it must be set with unique/exact colors-values. " \
              "<a href='https://smbyc.github.io/ThRasE/#thematic-raster-to-edit'>" \
              "See more</a>.<br/><br/>" \
              "Allow ThRasE apply an automatic classification symbology to this layer{band}?" \
            .format(layer=layer.name(), band=" in the band {}".format(band) if layer.bandCount() > 1 else "")
        reply = QMessageBox.question(None, 'Reading the symbology layer style...', msg, QMessageBox.Apply, QMessageBox.Cancel)
        if reply == QMessageBox.Apply:
            auto_symbology_classification_render(layer, band)
            return get_xml_style(layer, band)
        else:
            return
    return xml_style_items


def get_pixel_values(layer, band):
    xml_style_items = get_xml_style(layer, band)

    pixel_values = []
    for item_xml in xml_style_items:
        pixel_values.append(int(item_xml.get("value")))
    return pixel_values

# --------------------------------------------------------------------------


def chunks(l, n):
    """generate the sub-list of chunks of n-sizes from list l"""
    for i in range(0, len(l), n):
        yield l[i:i + n]


def pixel_count_in_chunk(args):
    img_path, band, pixel_values, xoff, yoff, xsize, ysize = args
    pixel_count = [0] * len(pixel_values)
    gdal_file = gdal.Open(img_path, gdal.GA_ReadOnly)

    chunk_narray = gdal_file.GetRasterBand(band).ReadAsArray(xoff, yoff, xsize, ysize).astype(np.int)

    for idx, pixel_value in enumerate(pixel_values):
        pixel_count[idx] += (chunk_narray == int(pixel_value)).sum()
    return pixel_count


@wait_process
def get_pixel_count_by_pixel_values(layer, band, pixel_values=None):
    """Get the total pixel count for each pixel values"""

    if pixel_values is None:
        pixel_values = get_pixel_values(layer, band)

    # split the image in chunks, the 0,0 is left-upper corner
    gdal_file = gdal.Open(get_file_path_of_layer(layer), gdal.GA_ReadOnly)
    chunk_size = 1000
    input_data = []
    for y in chunks(range(gdal_file.RasterYSize), chunk_size):
        yoff = y[0]
        ysize = len(y)
        for x in chunks(range(gdal_file.RasterXSize), chunk_size):
            xoff = x[0]
            xsize = len(x)

            input_data.append((get_file_path_of_layer(layer), band, pixel_values, xoff, yoff, xsize, ysize))

    # compute and merge all parallel process returns in one result
    with multiprocessing.Pool(multiprocessing.cpu_count()) as pool:
        imap_it = pool.imap(pixel_count_in_chunk, input_data)
        pixel_counts = np.sum([proc for proc in imap_it], axis=0).tolist()
        return dict(zip(pixel_values, pixel_counts))

# --------------------------------------------------------------------------
