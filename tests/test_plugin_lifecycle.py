"""Plugin teardown across multiple editing sessions and hidden dialogs."""

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent

from ThRasE.core.editing import LayerToEdit
from ThRasE.gui.main_dialog import ThRasEDialog
from ThRasE.gui.navigation_dialog import NavigationDialog
from ThRasE.thrase import ThRasE


def test_unload_disposes_hidden_navigation_dialogs_for_every_target(plugin, editable_raster, editing_ui, monkeypatch):
    removed_actions = []
    # pytest-qgis supplies a partial iface without menu-removal methods.
    monkeypatch.setattr(plugin.iface, "removePluginMenu", lambda *args: removed_actions.append(args), raising=False)
    monkeypatch.setattr(plugin.iface, "removeToolBarIcon", lambda *args: removed_actions.append(args), raising=False)
    dialog, view = editing_ui
    first_nav = NavigationDialog(dialog, layer_to_edit=editable_raster)
    editable_raster.navigation_dialog = first_nav
    other = LayerToEdit(editable_raster.qgs_layer, 2)
    second_nav = NavigationDialog(dialog, layer_to_edit=other)
    other.navigation_dialog = second_nav
    assert not first_nav.isVisible() and not second_nav.isVisible()
    dialog.closingPlugin.connect(plugin.onClosePlugin)
    plugin.unload()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(first_nav)
    assert sip.isdeleted(second_nav)
    assert sip.isdeleted(view)
    assert sip.isdeleted(dialog)
    assert ThRasE.dialog is None
    assert LayerToEdit.current is None
    assert not LayerToEdit.instances
    assert not ThRasEDialog.view_widgets
    assert len(removed_actions) == 3
