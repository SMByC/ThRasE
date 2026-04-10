# -*- coding: utf-8 -*-
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
import pytest

from qgis.core import QgsPointXY, QgsGeometry, QgsRectangle
from qgis.gui import QgsMapCanvas

from ThRasE.core.editing import LayerToEdit
from ThRasE.core.navigation import Navigation, NavigationTile
from ThRasE.utils.qgis_utils import load_layer


class _DummyWidget:
    """Generic stub that returns False for isChecked and no-ops for setEnabled."""
    def isChecked(self):
        return False

    def setEnabled(self, *args):
        pass


class _DummyRenderWidget:
    """Minimal render widget stub with a real QgsMapCanvas for navigation tests."""
    def __init__(self):
        self.canvas = QgsMapCanvas()

    def refresh(self):
        pass


class _DummyNavigationDialog:
    """Minimal navigation dialog stub so build_navigation can create rubber bands."""
    def __init__(self):
        self.render_widget = _DummyRenderWidget()
        self.highlight_tile = None
        self.SliderNavigationBlock = _DummyWidget()


@pytest.fixture
def nav_layer(plugin, thrase_dialog):
    """Load the test raster and return a LayerToEdit with a stubbed navigation dialog."""
    src = pytest.tests_data_dir / "test_data.tif"
    layer = load_layer(str(src), name="test_data_nav")
    assert layer is not None and layer.isValid()

    lte = LayerToEdit(layer, band=1)
    lte.setup_pixel_table()
    lte.navigation_dialog = _DummyNavigationDialog()
    LayerToEdit.current = lte
    return lte


# ---------------------------------------------------------------------------
# Tests for NavigationTile
# ---------------------------------------------------------------------------

class TestNavigationTile:
    def test_tile_extent(self):
        """NavigationTile stores the correct extent."""
        from qgis.PyQt.QtGui import QColor
        tile = NavigationTile(1, 100, 200, 300, 400, QColor("blue"))
        assert tile.idx == 1
        assert tile.xmin == 100
        assert tile.xmax == 200
        assert tile.ymin == 300
        assert tile.ymax == 400
        assert tile.extent == QgsRectangle(100, 300, 200, 400)

    def test_tile_is_valid_whole(self):
        """All tiles are valid when nav_type is 'whole'."""
        from qgis.PyQt.QtGui import QColor
        tile = NavigationTile(1, 0, 10, 0, 10, QColor("blue"))

        class _FakeNav:
            nav_type = "whole"

        assert tile.is_valid(_FakeNav()) is True

    def test_tile_is_valid_points(self):
        """All tiles are valid when nav_type is 'points'."""
        from qgis.PyQt.QtGui import QColor
        tile = NavigationTile(1, 0, 10, 0, 10, QColor("blue"))

        class _FakeNav:
            nav_type = "points"

        assert tile.is_valid(_FakeNav()) is True

    def test_tile_is_valid_polygons_intersects(self):
        """Tile is valid when it intersects a polygon."""
        from qgis.PyQt.QtGui import QColor
        tile = NavigationTile(1, 0, 10, 0, 10, QColor("blue"))
        polygon = QgsGeometry.fromPolygonXY([[
            QgsPointXY(5, 5), QgsPointXY(15, 5),
            QgsPointXY(15, 15), QgsPointXY(5, 15), QgsPointXY(5, 5)
        ]])

        class _FakeNav:
            nav_type = "polygons"
            polygons = [polygon]

        assert tile.is_valid(_FakeNav()) is True

    def test_tile_is_valid_polygons_no_intersect(self):
        """Tile is not valid when it does not intersect any polygon."""
        from qgis.PyQt.QtGui import QColor
        tile = NavigationTile(1, 0, 10, 0, 10, QColor("blue"))
        polygon = QgsGeometry.fromPolygonXY([[
            QgsPointXY(50, 50), QgsPointXY(60, 50),
            QgsPointXY(60, 60), QgsPointXY(50, 60), QgsPointXY(50, 50)
        ]])

        class _FakeNav:
            nav_type = "polygons"
            polygons = [polygon]

        assert tile.is_valid(_FakeNav()) is False


# ---------------------------------------------------------------------------
# Tests for Navigation.build_navigation (whole thematic file)
# ---------------------------------------------------------------------------

class TestBuildNavigationWhole:
    def test_build_whole_horizontal(self, nav_layer):
        """Build navigation over the whole thematic file in horizontal mode produces tiles."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        # tile size = half the extent width to get at least 2 columns
        tile_size = (extent.xMaximum() - extent.xMinimum()) / 2

        result = nav.build_navigation(tile_size, "horizontal")

        assert result is True
        assert nav.nav_type == "whole"
        assert len(nav.tiles) > 0
        assert nav.current_tile is not None
        assert nav.current_tile.idx == 1

    def test_build_whole_vertical(self, nav_layer):
        """Build navigation over the whole thematic file in vertical mode produces tiles."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        tile_size = (extent.yMaximum() - extent.yMinimum()) / 2

        result = nav.build_navigation(tile_size, "vertical")

        assert result is True
        assert nav.nav_type == "whole"
        assert len(nav.tiles) > 0
        assert nav.current_tile.idx == 1

    def test_tiles_cover_extent(self, nav_layer):
        """The union of all tile extents covers the entire layer extent."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        tile_size = (extent.xMaximum() - extent.xMinimum()) / 3

        nav.build_navigation(tile_size, "horizontal")

        tiles_extent = QgsRectangle()
        for tile in nav.tiles:
            tiles_extent.combineExtentWith(tile.extent)

        assert tiles_extent.contains(extent)

    def test_tile_indices_are_sequential(self, nav_layer):
        """Tile indices start at 1 and are sequential."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        tile_size = (extent.xMaximum() - extent.xMinimum()) / 2

        nav.build_navigation(tile_size, "horizontal")

        indices = [tile.idx for tile in nav.tiles]
        assert indices == list(range(1, len(nav.tiles) + 1))

    def test_single_tile_when_size_exceeds_extent(self, nav_layer):
        """A tile size larger than the extent produces exactly one tile."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        # tile size much larger than the extent
        tile_size = max(extent.xMaximum() - extent.xMinimum(),
                        extent.yMaximum() - extent.yMinimum()) * 10

        nav.build_navigation(tile_size, "horizontal")

        assert len(nav.tiles) == 1
        assert nav.current_tile.idx == 1


# ---------------------------------------------------------------------------
# Tests for Navigation.build_navigation (polygons)
# ---------------------------------------------------------------------------

class TestBuildNavigationPolygons:
    def test_build_with_polygon(self, nav_layer):
        """Build navigation with a polygon only produces tiles that intersect it."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        # polygon covering the top-left quarter of the extent
        cx = (extent.xMinimum() + extent.xMaximum()) / 2
        cy = (extent.yMinimum() + extent.yMaximum()) / 2
        polygon = QgsGeometry.fromPolygonXY([[
            QgsPointXY(extent.xMinimum(), cy),
            QgsPointXY(cx, cy),
            QgsPointXY(cx, extent.yMaximum()),
            QgsPointXY(extent.xMinimum(), extent.yMaximum()),
            QgsPointXY(extent.xMinimum(), cy),
        ]])

        tile_size = (extent.xMaximum() - extent.xMinimum()) / 4
        result = nav.build_navigation(tile_size, "horizontal", polygons=[polygon])

        assert result is True
        assert nav.nav_type == "polygons"
        assert len(nav.tiles) > 0
        # all tiles must intersect the polygon
        for tile in nav.tiles:
            assert polygon.intersects(tile.extent), \
                f"Tile {tile.idx} does not intersect the polygon"

    def test_polygon_outside_extent_produces_no_tiles(self, nav_layer):
        """A polygon completely outside the layer extent produces no tiles."""
        nav = nav_layer.navigation
        # polygon far away from the layer extent
        polygon = QgsGeometry.fromPolygonXY([[
            QgsPointXY(0, 0), QgsPointXY(1, 0),
            QgsPointXY(1, 1), QgsPointXY(0, 1), QgsPointXY(0, 0)
        ]])

        result = nav.build_navigation(1000, "horizontal", polygons=[polygon])

        assert not result
        assert len(nav.tiles) == 0


# ---------------------------------------------------------------------------
# Tests for Navigation.build_navigation (points)
# ---------------------------------------------------------------------------

class TestBuildNavigationPoints:
    def test_build_with_points(self, nav_layer):
        """Build navigation with points creates one tile per point inside the extent."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        cx = (extent.xMinimum() + extent.xMaximum()) / 2
        cy = (extent.yMinimum() + extent.yMaximum()) / 2

        points = [QgsPointXY(cx, cy),
                  QgsPointXY(cx - 1000, cy + 1000)]

        tile_size = 5000
        result = nav.build_navigation(tile_size, "horizontal", points=points)

        assert result is True
        assert nav.nav_type == "points"
        assert len(nav.tiles) == 2

    def test_points_outside_extent_are_skipped(self, nav_layer):
        """Points outside the layer extent are not included in navigation."""
        nav = nav_layer.navigation
        # points far away
        points = [QgsPointXY(0, 0), QgsPointXY(1, 1)]

        result = nav.build_navigation(1000, "horizontal", points=points)

        assert result is False
        assert len(nav.tiles) == 0

    def test_point_tiles_centered_on_points(self, nav_layer):
        """Each tile built from a point is centered on that point."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        cx = (extent.xMinimum() + extent.xMaximum()) / 2
        cy = (extent.yMinimum() + extent.yMaximum()) / 2
        point = QgsPointXY(cx, cy)

        tile_size = 5000
        nav.build_navigation(tile_size, "horizontal", points=[point])

        assert len(nav.tiles) == 1
        tile = nav.tiles[0]
        assert abs(tile.xmin - (cx - tile_size / 2)) < 1e-6
        assert abs(tile.xmax - (cx + tile_size / 2)) < 1e-6
        assert abs(tile.ymin - (cy - tile_size / 2)) < 1e-6
        assert abs(tile.ymax - (cy + tile_size / 2)) < 1e-6


# ---------------------------------------------------------------------------
# Tests for Navigation.set_current_tile and navigation state
# ---------------------------------------------------------------------------

class TestNavigationState:
    def test_set_current_tile(self, nav_layer):
        """set_current_tile updates the current tile index."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        tile_size = (extent.xMaximum() - extent.xMinimum()) / 3

        nav.build_navigation(tile_size, "horizontal")
        assert len(nav.tiles) > 1

        nav.set_current_tile(2)
        assert nav.current_tile.idx == 2

    def test_rebuild_clears_previous_tiles(self, nav_layer):
        """Building navigation a second time clears tiles from the first build."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()

        # first build with small tiles
        tile_size_small = (extent.xMaximum() - extent.xMinimum()) / 4
        nav.build_navigation(tile_size_small, "horizontal")
        first_count = len(nav.tiles)

        # second build with large tiles
        tile_size_large = (extent.xMaximum() - extent.xMinimum()) * 10
        nav.build_navigation(tile_size_large, "horizontal")
        second_count = len(nav.tiles)

        assert second_count == 1
        assert second_count < first_count

    def test_delete_clears_state(self, nav_layer):
        """delete() resets all navigation state."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        tile_size = (extent.xMaximum() - extent.xMinimum()) / 2

        nav.build_navigation(tile_size, "horizontal")
        assert len(nav.tiles) > 0

        nav.delete()

        assert nav.tiles == []
        assert nav.current_tile is None
        assert nav.is_valid is False

    def test_horizontal_vs_vertical_tile_order(self, nav_layer):
        """Horizontal and vertical modes produce the same tiles but in different order."""
        nav = nav_layer.navigation
        extent = nav_layer.extent()
        tile_size = (extent.xMaximum() - extent.xMinimum()) / 3

        nav.build_navigation(tile_size, "horizontal")
        horizontal_extents = {(t.xmin, t.xmax, t.ymin, t.ymax) for t in nav.tiles}
        h_order = [(t.xmin, t.ymin) for t in nav.tiles]

        nav.build_navigation(tile_size, "vertical")
        vertical_extents = {(t.xmin, t.xmax, t.ymin, t.ymax) for t in nav.tiles}
        v_order = [(t.xmin, t.ymin) for t in nav.tiles]

        # same tiles, different traversal order
        assert horizontal_extents == vertical_extents
        assert h_order != v_order
