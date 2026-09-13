import os
import sys
import types
from pathlib import Path

import pytest
from qgis.testing import start_app

# Pre-stub Qt resources module to avoid heavy import during plugin/module import
if "ThRasE.resources" not in sys.modules:
    sys.modules["ThRasE.resources"] = types.ModuleType("ThRasE.resources")

# Use pytest-qgis to bootstrap a QGIS app and iface
pytest_plugins = ("pytest_qgis",)
# Expose tests data dir
pytest.tests_data_dir = Path(__file__).parent.resolve() / "data"

_is_docker = os.environ.get("IS_DOCKER_CONTAINER", "")[:1].lower() in ("t", "y", "1")

if _is_docker:
    # when running in a docker container, we use the start_app provided by qgis rather
    # than that of pytest-qgis. pytest-qgis does not clean up the application properly
    # and results in a seg-fault
    print("RUNNING IN DOCKER CONTAINER")
    start_app()


def pytest_sessionfinish(session, exitstatus):
    """Force-exit after tests complete in Docker to avoid the Qt/OpenGL cleanup segfault."""
    if _is_docker:
        os._exit(exitstatus)


class _MsgBar:
    def pushMessage(self, *args, **kwargs):
        pass

    def clearWidgets(self):
        pass

    def createMessage(self, *args):
        from qgis.PyQt.QtWidgets import QHBoxLayout, QWidget

        w = QWidget()
        QHBoxLayout(w)
        return w

    def pushWidget(self, *args, **kwargs):
        pass


class _Label:
    def setText(self, *args, **kwargs):
        pass


class _Checkable:
    def setChecked(self, *args, **kwargs):
        pass


class _RegistryWidget:
    def __init__(self):
        self.showAll = _Checkable()

    def update_registry(self):
        pass

    def isVisible(self):
        return False


class _EnableDisable:
    def setEnabled(self, *args):
        pass

    def isChecked(self):
        return False


class _SynchronousRasterRecodeController:
    """Run the task payload inline for dialog integration tests."""

    def __init__(self):
        self.last_result = None

    def start(self, request, layer_to_edit, **kwargs):
        from dataclasses import replace

        from qgis.core import QgsVectorLayerFeatureSource

        from ThRasE.core.raster_recode import RecodeStatus, finalize_commit
        from ThRasE.gui.raster_recode_task import stage_recode_with_qgis_mask
        from ThRasE.utils.qgis_utils import commit_staged_and_reload

        vector_mask_layer = kwargs.get("vector_mask_layer")
        vector_mask_source = QgsVectorLayerFeatureSource(vector_mask_layer) if vector_mask_layer is not None else None
        registry_pixels = tuple(layer_to_edit.pixel_log_store)
        request = replace(
            request,
            registry_points=tuple((pixel.x(), pixel.y()) for pixel in registry_pixels),
        )
        result = stage_recode_with_qgis_mask(
            request,
            vector_mask_source,
            progress_callback=lambda _value: None,
            is_cancelled=lambda: False,
        )
        self.last_result = result
        if result.status is RecodeStatus.NO_CHANGES:
            callback = kwargs.get("on_no_changes")
            if callback:
                callback(result)
            return True
        receipt, _restored_layers = commit_staged_and_reload(result, additional_layers=(layer_to_edit.qgs_layer,))
        finalize_commit(receipt)
        if request.collect_changes and result.changes is not None:
            layer_to_edit.store_global_edit_changes(result.changes, result.geotransform)
        else:
            layer_to_edit.reconcile_registry(result.registry_values, registry_pixels)
        callback = kwargs.get("on_success")
        if callback:
            callback(replace(result, status=RecodeStatus.COMMITTED, stage_path=None, lock_path=None, lock_token=None))
        return True


class DummyDialog:
    """A minimal ThRasE.dialog stub needed by core editing functions during tests."""

    def __init__(self):
        self.MsgBar = _MsgBar()
        self.registry_widget = _RegistryWidget()
        self.editing_status = _Label()
        # Generic placeholders referenced by other code paths (avoid AttributeError)
        self.grid_columns = 1
        self.grid_rows = 1
        # Navigation stubs
        self.NavigationBlockWidgetControls = _EnableDisable()
        self.currentTileKeepVisible = _EnableDisable()
        self.raster_recode_controller = _SynchronousRasterRecodeController()


@pytest.fixture
def plugin(pytestconfig, qgis_iface, qgis_parent, qgis_new_project):
    """Initialize and return the plugin instance using pytest-qgis fixtures.

    The plugin GUI is registered but we avoid running modal dialogs in tests.
    """
    from ThRasE import classFactory

    plugin = classFactory(qgis_iface)
    plugin.initGui()
    yield plugin
    try:
        plugin.removes_temporary_files()
    except Exception:
        pass


@pytest.fixture
def thrase_dialog(plugin):
    """Provide a lightweight `ThRasE.dialog` stub for headless tests.

    Many core functions expect `ThRasE.dialog` to exist. We install a minimal
    object satisfying the methods/attributes touched by tests.
    """
    _ThRasE = plugin.__class__
    _ThRasE.dialog = DummyDialog()
    try:
        yield _ThRasE.dialog
    finally:
        _ThRasE.dialog = None


@pytest.fixture
def load_yaml_mapping():
    """Return a function that parses the provided ThRasE YAML and extracts the recode mapping {old:new}."""
    import yaml

    def _loader(yaml_path: Path):
        # Use an unsafe loader to support legacy !!python/object/apply:collections.OrderedDict dumped files
        with open(yaml_path) as f:
            try:
                # Prefer yaml.unsafe_load if available (PyYAML >=5.1)
                unsafe_load = getattr(yaml, "unsafe_load", None)
                if unsafe_load:
                    data = unsafe_load(f)
                else:
                    # Fallback to explicit UnsafeLoader or base Loader if unavailable
                    UnsafeLoader = getattr(yaml, "UnsafeLoader", getattr(yaml, "Loader", None))
                    data = yaml.load(f, Loader=UnsafeLoader)
            except Exception:
                # Retry with the broadest available loader
                f.seek(0)
                UnsafeLoader = getattr(yaml, "UnsafeLoader", getattr(yaml, "Loader", None))
                data = yaml.load(f, Loader=UnsafeLoader)
        # The file may use an OrderedDict dumped as a list of (key,value) pairs; convert to dict
        if isinstance(data, list):
            # Build mapping from sequence of pairs
            cfg = {}
            for pair in data:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    cfg[pair[0]] = pair[1]
            data = cfg
        # recode table may be stored as list of dicts with keys value/new_value
        recode = data.get("recode_pixel_table", [])
        mapping = {int(item["value"]): int(item["new_value"]) for item in recode if item.get("new_value") is not None}
        return data, mapping

    return _loader
