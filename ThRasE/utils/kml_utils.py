"""
/***************************************************************************
 ThRasE

 A powerful and fast thematic raster editor Qgis plugin
                              -------------------
        copyright            : (C) 2019-2026 by Xavier Corredor Llano, SMByC
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

from html import escape


def write_google_earth_kml(path, placemark_name, description, xmin, ymin, xmax, ymax):
    """Serialize a KML navigation-tile Placemark using a raw string template.

    Escaping uses a clear two-stage approach so that injection through the
    layer name is impossible while trusted ``<b>``, ``<em>`` and ``<br/>``
    tags survive the XML round-trip:

    **Stage 1** (done by the caller):
        - ``html.escape(layer_name)`` escapes the only untrusted input.
        - The escaped name is interpolated into ``description_html`` alongside
          the trusted formatting tags.

    **Stage 2** (done here):
        - ``description_html`` is XML-text-escaped (``&`` → ``&amp;``,
          ``<`` → ``&lt;``, ``>`` → ``&gt;``) before interpolation into
          ``<description>``.  After a standards-compliant XML parser
          unescapes the entities the trusted markup is reconstructed.

    The result is written as UTF-8 text to *path*.
    """
    kml_ns = "http://www.opengis.net/kml/2.2"

    # --- Stage 2: XML-text-escape the full description HTML ---
    # The caller already HTML-escaped the layer name.  We XML-escape the
    # entire blob so angle-brackets from trusted tags become &lt; / &gt;
    # in the serialised form, guaranteeing well-formed XML.  Parser
    # unescaping restores the original markup.
    description_xml_safe = escape(description, quote=False)

    # Placemark name also gets XML-text-escaped as a defence-in-depth
    # measure (it is a code-generated integer constant in practice, so
    # this is a no-op for the current callers).
    placemark_name_xml_safe = escape(placemark_name, quote=False)

    # Coordinate string — all values come from QGIS CRS transforms so they
    # are already trusted numerics.
    coord_text = (
        f"\n{xmin},{ymin},1000\n{xmin},{ymax},1000\n"
        f"{xmax},{ymax},1000\n{xmax},{ymin},1000\n{xmin},{ymin},1000\n"
    )

    kml_template = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<kml xmlns="{kml_ns}">\n'
        "  <Document>\n"
        '    <Style id="transBluePoly">\n'
        "      <LineStyle>\n"
        "        <width>1.5</width>\n"
        "      </LineStyle>\n"
        "      <PolyStyle>\n"
        "        <color>00000000</color>\n"
        "      </PolyStyle>\n"
        "    </Style>\n"
        "    <Placemark>\n"
        f"      <name>{placemark_name_xml_safe}</name>\n"
        f"      <description>{description_xml_safe}</description>\n"
        '      <styleUrl>#transBluePoly</styleUrl>\n'
        "      <Polygon>\n"
        "        <extrude>1</extrude>\n"
        "        <altitudeMode>relativeToGround</altitudeMode>\n"
        "        <outerBoundaryIs>\n"
        "          <LinearRing>\n"
        f"            <coordinates>{coord_text}</coordinates>\n"
        "          </LinearRing>\n"
        "        </outerBoundaryIs>\n"
        "      </Polygon>\n"
        "    </Placemark>\n"
        "  </Document>\n"
        "</kml>\n"
    )

    with open(path, "w", encoding="utf-8") as outfile:
        outfile.write(kml_template)
