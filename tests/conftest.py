import os
import sys
import types
import shutil
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

if os.environ.get("IS_DOCKER_CONTAINER") and os.environ["IS_DOCKER_CONTAINER"].lower()[
    0
] in ["t", "y", "1"]:
    # when running in a docker container, we use the start_app provided by qgis rather
    # than that of pytest-qgis. pytest-qgis does not clean up the application properly
    # and results in a seg-fault
    print("RUNNING IN DOCKER CONTAINER")
    start_app()

class _MsgBar:
    def pushMessage(self, *args, **kwargs):
        # no-op in tests
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


class DummyDialog:
    """A minimal ThRasE.dialog stub needed by core editing functions during tests."""
    def __init__(self):
        self.MsgBar = _MsgBar()
        self.registry_widget = _RegistryWidget()
        self.editing_status = _Label()
        # Generic placeholders referenced by other code paths (avoid AttributeError)
        self.grid_columns = 1
        self.grid_rows = 1


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
        with open(yaml_path, "r") as f:
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

