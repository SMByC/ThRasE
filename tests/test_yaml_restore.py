from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle

from ThRasE.core.editing import LayerToEdit
from ThRasE.gui import main_dialog


def _pixel(value, label, color, new_value=None, visible=True):
    return {
        "value": value,
        "label": label,
        "color": dict(zip(("R", "G", "B", "A"), color, strict=True)),
        "new_value": new_value,
        "s/h": visible,
    }


class _Layer:
    def name(self):
        return "thematic"


class _MessageBox:
    class Icon:
        Question = object()

    class ButtonRole:
        AcceptRole = object()
        RejectRole = object()

    class StandardButton:
        Cancel = object()

    response = "config"

    def __init__(self, _parent):
        self.buttons = []
        self.escape_button = None

    def setIcon(self, _icon):
        pass

    def setWindowTitle(self, _title):
        pass

    def setText(self, _text):
        pass

    def addButton(self, text, _role=None):
        button = object()
        self.buttons.append((text, button))
        return button

    def setDefaultButton(self, _button):
        pass

    def setEscapeButton(self, _button):
        self.escape_button = _button

    def exec(self):
        pass

    def clickedButton(self):
        if self.response == "layer":
            return self.buttons[0][1]
        if self.response == "config":
            return self.buttons[1][1]
        if self.response == "cancel":
            return self.buttons[2][1]
        if self.response == "escape":
            assert self.escape_button is self.buttons[2][1]
            return self.escape_button
        return None


def _config(saved_pixels, **extra):
    config = {
        "thematic_file_to_edit": {"path": "thematic.tif"},
        "config_file": "restored.yaml",
        "recode_pixel_table": saved_pixels,
        "view_widgets": [],
        "navigation": {"type": "free"},
        "num_layer_toolbars_per_view": 1,
    }
    config.update(extra)
    return config


def _restore(monkeypatch, live_pixels, config, *, response=None):
    layer = _Layer()
    restored = SimpleNamespace(
        pixels=deepcopy(live_pixels),
        pixels_backup=None,
        symbology=None,
        config_file=None,
        setup_symbology=lambda: setattr(
            restored,
            "symbology",
            [
                (
                    p["label"] or str(p["value"]),
                    p["value"],
                    (*tuple(p["color"][k] for k in ("R", "G", "B")), 255 if p["s/h"] else 0),
                )
                for p in restored.pixels
            ],
        ),
    )
    LayerToEdit.current = restored

    dialog = SimpleNamespace()
    dialog.QCBox_LayerToEdit = SimpleNamespace(
        currentLayer=lambda: layer,
        blockSignals=lambda _enabled: None,
    )
    dialog.update_save_buttons_state = lambda: None
    dialog.set_recode_pixel_table = lambda: None
    dialog.update_recode_pixel_table = lambda: None
    dialog.QCBox_NumLayerToolbars = SimpleNamespace(setCurrentIndex=lambda _index: None)
    dialog.set_layer_toolbars = lambda _index: None
    dialog.select_layer_to_edit = lambda *_args, **_kwargs: setattr(LayerToEdit, "current", restored)
    monkeypatch.setattr(main_dialog, "load_and_select_layer_in", lambda *_args, **_kwargs: layer)
    monkeypatch.setattr(main_dialog.ThRasEDialog, "view_widgets", [])
    if response is not None:
        _MessageBox.response = response
        monkeypatch.setattr(main_dialog, "QMessageBox", _MessageBox)

    result = main_dialog.ThRasEDialog.restore_config.__wrapped__.__wrapped__(dialog, "restored.yaml", config)
    return restored, result


def test_empty_labels_merge_live_labels_and_preserve_saved_state(monkeypatch):
    live = [_pixel(1, "live one", (1, 2, 3, 255)), _pixel(2, "live two", (4, 5, 6, 255))]
    saved = [_pixel(2, "", (20, 21, 22, 255), 9, False), _pixel(1, "", (30, 31, 32, 255), 8, True)]
    backup = deepcopy(saved)
    restored, result = _restore(monkeypatch, live, _config(saved, recode_pixel_table_backup=backup))
    assert result is None

    assert [p["value"] for p in restored.pixels] == [2, 1]
    assert [p["label"] for p in restored.pixels] == ["live two", "live one"]
    assert [p["color"] for p in restored.pixels] == [p["color"] for p in saved]
    assert [p["new_value"] for p in restored.pixels] == [9, 8]
    assert [p["label"] for p in restored.pixels_backup] == ["live two", "live one"]


def test_whitespace_labels_use_legacy_merge_and_derived_backup(monkeypatch):
    live = [_pixel(1, "live", (1, 2, 3, 255))]
    saved = [_pixel(1, "   ", (20, 21, 22, 255), 9, False)]
    restored, result = _restore(monkeypatch, live, _config(saved))
    assert result is None

    assert restored.pixels[0]["label"] == "live"
    assert restored.pixels[0]["color"] == saved[0]["color"]
    assert restored.pixels_backup[0]["label"] == "live"
    assert restored.pixels_backup[0]["new_value"] is None


@pytest.mark.parametrize("response", ["config", "layer"])
def test_label_conflict_choices_restore_expected_pixels(monkeypatch, response):
    live = [_pixel(1, "live", (1, 2, 3, 255), None, True)]
    saved = [_pixel(1, "saved", (20, 21, 22, 255), 7, False)]
    restored, result = _restore(
        monkeypatch,
        live,
        _config(saved, symbology=[("saved", 1, (20, 21, 22, 255))]),
        response=response,
    )
    assert result is None

    if response == "config":
        assert restored.pixels[0]["label"] == "saved"
        assert restored.pixels[0]["color"] == saved[0]["color"]
    else:
        assert restored.pixels[0]["label"] == "live"
        assert restored.pixels[0]["new_value"] == 7
        assert restored.pixels[0]["s/h"] is False


@pytest.mark.parametrize("response", ["cancel", "escape", "dismissed"])
def test_label_conflict_cancel_or_dismissal_aborts_restore(monkeypatch, response):
    live = [_pixel(1, "live", (1, 2, 3, 255))]
    saved = [_pixel(1, "saved", (20, 21, 22, 255), 7, False)]
    restored, result = _restore(monkeypatch, live, _config(saved), response=response)

    assert restored.pixels == live
    assert restored.symbology is None
    assert result is False


def test_legacy_restore_rebuilds_renderer_and_backup(monkeypatch):
    live = [_pixel(1, "live", (1, 2, 3, 255))]
    saved = [_pixel(1, "saved", (20, 21, 22, 255), 7, False)]
    restored, result = _restore(monkeypatch, live, _config(saved), response="config")
    assert result is None

    assert restored.symbology == [("saved", 1, (20, 21, 22, 0))]
    assert restored.pixels_backup[0]["new_value"] is None


def test_empty_labels_merge_classes_by_value_when_class_sets_differ(monkeypatch):
    live = [_pixel(1, "live one", (1, 2, 3, 255)), _pixel(3, "live three", (4, 5, 6, 255))]
    saved = [_pixel(2, "", (20, 21, 22, 255), 9, False), _pixel(1, "", (30, 31, 32, 255), 8, True)]
    restored, result = _restore(monkeypatch, live, _config(saved))
    assert result is None

    assert [p["value"] for p in restored.pixels] == [2, 1]
    assert [p["label"] for p in restored.pixels] == ["", "live one"]
    assert [p["label"] for p in restored.pixels_backup] == ["", "live one"]
    assert [p["color"] for p in restored.pixels] == [p["color"] for p in saved]


def test_equal_mixed_labels_use_saved_config_without_dialog(monkeypatch):
    live = [_pixel(1, "one", (1, 2, 3, 255)), _pixel(2, "", (4, 5, 6, 255))]
    saved = [_pixel(1, "one", (20, 21, 22, 255), 7, False), _pixel(2, "", (30, 31, 32, 255), None, True)]
    restored, result = _restore(
        monkeypatch,
        live,
        _config(saved, symbology=[("one", 1, (20, 21, 22, 0)), ("2", 2, (30, 31, 32, 255))]),
    )
    assert result is None

    assert restored.pixels == saved
    assert restored.symbology == [("one", 1, (20, 21, 22, 0)), ("2", 2, (30, 31, 32, 255))]


def test_layer_choice_keeps_live_classes_and_pristine_backup(monkeypatch):
    live = [
        _pixel(4, "live only", (7, 8, 9, 255), None, False),
        _pixel(3, "live three", (1, 2, 3, 255), None, True),
        _pixel(1, "live one", (4, 5, 6, 255), None, False),
    ]
    saved = [
        _pixel(2, "saved only", (40, 41, 42, 255), 6, True),
        _pixel(1, "saved one", (20, 21, 22, 255), 7, True),
        _pixel(3, "saved three", (30, 31, 32, 255), 8, False),
    ]
    restored, result = _restore(monkeypatch, live, _config(saved), response="layer")
    assert result is None

    assert [p["value"] for p in restored.pixels] == [4, 3, 1]
    assert [p["color"] for p in restored.pixels] == [p["color"] for p in live]
    assert [(p["new_value"], p["s/h"]) for p in restored.pixels] == [(None, False), (8, False), (7, True)]
    assert 2 not in [p["value"] for p in restored.pixels]
    assert restored.pixels_backup == [{**p, "new_value": None} for p in live]


def test_restore_recode_table_rebuilds_old_new_value(monkeypatch):
    restored = SimpleNamespace(
        pixels=[_pixel(1, "one", (1, 2, 3, 255), 99), _pixel(2, "two", (4, 5, 6, 255), 88)],
        pixels_backup=[_pixel(1, "one", (1, 2, 3, 255), 5), _pixel(2, "two", (4, 5, 6, 255), None)],
        symbology=[("one", 1, (1, 2, 3, 255)), ("two", 2, (4, 5, 6, 255))],
        setup_symbology=lambda: None,
        qgs_layer=object(),
        band=1,
        old_new_value={1: 99, 2: 88},
    )
    LayerToEdit.current = restored
    class _Item:
        def __init__(self, text, checked=True):
            self._text = text
            self._checked = checked

        def text(self):
            return self._text

        def checkState(self):
            return 2 if self._checked else 0

    def set_table():
        dialog.recodePixelTable = SimpleNamespace(
            item=lambda row, column: _Item("5" if row == 0 and column == 3 else "", column != 1 or row == 0)
        )

    dialog = SimpleNamespace(
        set_recode_pixel_table=set_table,
        update_recode_pixel_table=lambda: main_dialog.ThRasEDialog.update_recode_pixel_table.__wrapped__(dialog),
        NavigationBlockWidget=SimpleNamespace(setEnabled=lambda _value: None),
        QGBox_GlobalEditTools=SimpleNamespace(setEnabled=lambda _value: None),
        QLbl_NumberClassesToEdit=SimpleNamespace(setText=lambda _text: None),
    )
    dialog.view_widgets = []
    monkeypatch.setattr(main_dialog, "apply_symbology", lambda *_args: None)

    main_dialog.ThRasEDialog.restore_recode_table.__wrapped__(dialog)
    assert [p["new_value"] for p in restored.pixels] == [5, None]
    assert restored.old_new_value == {1: 5}


@pytest.mark.parametrize("restore_result", [False, None])
def test_loaded_settings_cancellation_lifecycle(monkeypatch, restore_result):
    close_calls = []
    restore_calls = []
    dialog = SimpleNamespace(
        restore_config=lambda path, config: restore_calls.append((path, config)) or restore_result,
        close=lambda: close_calls.append(True),
    )

    result = main_dialog.ThRasEDialog._restore_loaded_settings(dialog, "settings.yaml", {"test": True})

    assert result is (restore_result is not False)
    assert restore_calls == [("settings.yaml", {"test": True})]
    assert close_calls == ([True] if restore_result is False else [])


@pytest.mark.parametrize(
    "layer_name",
    [
        "Land & Water",
        "</description><NetworkLink>evil</NetworkLink>",
        "</em><script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "ampersand & more",
        "Caf\u00e9 \u2013 r\u00e9sum\u00e9",
    ],
)
def test_google_earth_kml_escapes_layer_name_in_xml_and_html(monkeypatch, tmp_path, layer_name):
    """Generated raw KML must be well-formed and injection-safe.

    The two-stage escaping (HTML then XML-text) ensures the layer name can
    never break out of the <description> element, while trusted <b>/<em>/<br/>
    formatting tags survive the XML round-trip.
    """
    import xml.etree.ElementTree as ET

    from ThRasE.thrase import ThRasE

    tile = SimpleNamespace(idx=1, extent=QgsRectangle(0, 0, 1, 1))
    layer = SimpleNamespace(
        name=lambda: layer_name,
        crs=lambda: QgsCoordinateReferenceSystem("EPSG:4326"),
    )
    current = SimpleNamespace(
        navigation=SimpleNamespace(current_tile=tile, tiles=[tile]),
        qgs_layer=layer,
    )
    LayerToEdit.current = current
    ThRasE.tmp_dir = str(tmp_path)
    opened = []
    monkeypatch.setattr(main_dialog, "open_file", opened.append)

    main_dialog.ThRasEDialog.open_current_tile_navigation_in_google_earth.__wrapped__(SimpleNamespace())

    kml_path = opened[0]
    kml_text = Path(kml_path).read_text(encoding="utf-8")

    # Parse the KML — must be well-formed XML.
    kml_ns = "http://www.opengis.net/kml/2.2"
    root = ET.fromstring(kml_text)
    desc_elem = root.find(f".//{{{kml_ns}}}description")
    assert desc_elem is not None, "Could not find a KML <description> element"

    # The <description> must have no child elements — text content only.
    assert len(desc_elem) == 0, "Description element contains child XML elements"

    desc_text = desc_elem.text or ""

    # Trusted formatting tags survive the round-trip (XML-escaped in the
    # source, restored by the parser).
    assert "<b>1</b>" in desc_text, "expected <b>1</b> in description text"
    assert "<em>" in desc_text, "expected <em> in description text"
    assert "<br/>" in desc_text, "expected <br/> in description text"

    # Malicious payloads must NOT appear as unescaped HTML/XML tags.
    for dangerous in ("</description>", "<NetworkLink>", "<script>", "<img "):
        assert dangerous not in desc_text, f"unescaped '{dangerous}' in description text"

    # The layer name text survives, but its dangerous characters are encoded.
    if "&" in layer_name:
        assert "&amp;" in desc_text
    if ">" in layer_name:
        assert "&gt;" in desc_text
    if "Café" in layer_name:
        assert "Café" in desc_text
