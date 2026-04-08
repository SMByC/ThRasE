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

from qgis.PyQt.QtWidgets import QTableWidgetItem

from ThRasE.core.editing import LayerToEdit
from ThRasE.gui.autofill_dialog import AutoFill


@pytest.fixture
def autofill_dialog(qgis_parent):
    """Create an AutoFill dialog instance for testing."""
    dialog = AutoFill(parent=qgis_parent)
    return dialog


@pytest.fixture
def setup_pixels(plugin, thrase_dialog):
    """Set up a minimal LayerToEdit.current with pixel data for autofill tests."""
    from ThRasE.utils.qgis_utils import load_layer

    src = pytest.tests_data_dir / "test_data.tif"
    layer = load_layer(str(src), name="test_data_autofill")
    assert layer is not None and layer.isValid()

    lte = LayerToEdit(layer, band=1)
    lte.setup_pixel_table()
    LayerToEdit.current = lte
    # stub the dialog methods called at the end of apply_autofill
    from ThRasE.thrase import ThRasE
    ThRasE.dialog.set_recode_pixel_table = lambda: None
    ThRasE.dialog.update_recode_pixel_table = lambda: None
    return lte


# ---------------------------------------------------------------------------
# Tests for check_condition
# ---------------------------------------------------------------------------

class TestCheckCondition:
    def test_wildcard(self, autofill_dialog):
        assert autofill_dialog.check_condition("*") is True

    def test_empty_string(self, autofill_dialog):
        assert autofill_dialog.check_condition("") is False

    def test_none(self, autofill_dialog):
        assert autofill_dialog.check_condition(None) is False

    def test_valid_expression_uppercase_V(self, autofill_dialog):
        assert autofill_dialog.check_condition("V > 3") is True

    def test_valid_expression_lowercase_v(self, autofill_dialog):
        assert autofill_dialog.check_condition("v > 3") is True

    def test_valid_compound_expression(self, autofill_dialog):
        assert autofill_dialog.check_condition("V > 3 and V <= 8") is True

    def test_invalid_expression(self, autofill_dialog):
        assert autofill_dialog.check_condition("V >>><< invalid") is False

    def test_equality(self, autofill_dialog):
        assert autofill_dialog.check_condition("V == 35") is True


# ---------------------------------------------------------------------------
# Tests for check_value
# ---------------------------------------------------------------------------

class TestCheckValue:
    def test_empty_string(self, autofill_dialog):
        assert autofill_dialog.check_value("") is True

    def test_none(self, autofill_dialog):
        assert autofill_dialog.check_value(None) is True

    def test_literal_number(self, autofill_dialog):
        assert autofill_dialog.check_value("42") is True

    def test_expression_with_V(self, autofill_dialog):
        assert autofill_dialog.check_value("V + 1") is True

    def test_expression_with_lowercase_v(self, autofill_dialog):
        assert autofill_dialog.check_value("v + 1") is True

    def test_invalid_expression(self, autofill_dialog):
        assert autofill_dialog.check_value("not_a_valid + ??") is False


# ---------------------------------------------------------------------------
# Tests for apply_autofill
# ---------------------------------------------------------------------------

class TestApplyAutofill:
    def _set_autofill_rows(self, dialog, rows):
        """Helper: fill the AutoFillTable with (condition, value) rows."""
        dialog.AutoFillTable.setRowCount(len(rows))
        dialog.AutoFillTable.setColumnCount(2)
        for row_idx, (condition, value) in enumerate(rows):
            dialog.AutoFillTable.setItem(row_idx, 0, QTableWidgetItem(condition))
            dialog.AutoFillTable.setItem(row_idx, 1, QTableWidgetItem(value))

    def test_wildcard_sets_all(self, autofill_dialog, setup_pixels):
        """Wildcard condition '*' with a constant value sets all pixels."""
        self._set_autofill_rows(autofill_dialog, [("*", "99")])
        autofill_dialog.apply_autofill()

        for pixel in LayerToEdit.current.pixels:
            assert pixel["new_value"] == 99

    def test_empty_table_no_changes(self, autofill_dialog, setup_pixels):
        """An empty autofill table should not modify any pixels."""
        self._set_autofill_rows(autofill_dialog, [])
        autofill_dialog.apply_autofill()

        for pixel in LayerToEdit.current.pixels:
            assert pixel["new_value"] is None

    def test_condition_with_V_variable(self, autofill_dialog, setup_pixels):
        """Condition using V filters correctly; only matching pixels get new values."""
        # pixel values: [0, 34, 35, 36, 37, 38, 40, 42, 43, 44, 45, 46, 47, 49, 51, 52, 53]
        self._set_autofill_rows(autofill_dialog, [("V > 50", "10")])
        autofill_dialog.apply_autofill()

        for pixel in LayerToEdit.current.pixels:
            if pixel["value"] > 50:
                assert pixel["new_value"] == 10
            else:
                assert pixel["new_value"] is None

    def test_condition_with_lowercase_v(self, autofill_dialog, setup_pixels):
        """Lowercase 'v' should work identically to uppercase 'V'."""
        self._set_autofill_rows(autofill_dialog, [("v == 35", "99")])
        autofill_dialog.apply_autofill()

        for pixel in LayerToEdit.current.pixels:
            if pixel["value"] == 35:
                assert pixel["new_value"] == 99
            else:
                assert pixel["new_value"] is None

    def test_value_expression_with_V(self, autofill_dialog, setup_pixels):
        """The value column can use V to compute the new value from the pixel value."""
        self._set_autofill_rows(autofill_dialog, [("*", "V + 1")])
        autofill_dialog.apply_autofill()

        for pixel in LayerToEdit.current.pixels:
            assert pixel["new_value"] == pixel["value"] + 1

    def test_later_rules_overwrite_earlier(self, autofill_dialog, setup_pixels):
        """Rules are applied sequentially; later rules overwrite earlier ones."""
        self._set_autofill_rows(autofill_dialog, [
            ("*", "10"),       # first: set all to 10
            ("V == 35", "99"), # then: override pixel 35 to 99
        ])
        autofill_dialog.apply_autofill()

        for pixel in LayerToEdit.current.pixels:
            if pixel["value"] == 35:
                assert pixel["new_value"] == 99
            else:
                assert pixel["new_value"] == 10

    def test_compound_condition(self, autofill_dialog, setup_pixels):
        """Compound conditions like 'V >= 40 and V <= 45' work."""
        self._set_autofill_rows(autofill_dialog, [("V >= 40 and V <= 45", "7")])
        autofill_dialog.apply_autofill()

        for pixel in LayerToEdit.current.pixels:
            if 40 <= pixel["value"] <= 45:
                assert pixel["new_value"] == 7
            else:
                assert pixel["new_value"] is None

    def test_wildcard_with_empty_value_clears(self, autofill_dialog, setup_pixels):
        """Wildcard with empty value sets new_value to None (clears)."""
        # first set all to something
        for pixel in LayerToEdit.current.pixels:
            pixel["new_value"] = 99
        # then clear via autofill
        self._set_autofill_rows(autofill_dialog, [("*", "")])
        autofill_dialog.apply_autofill()

        for pixel in LayerToEdit.current.pixels:
            assert pixel["new_value"] is None
