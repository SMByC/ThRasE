import os
import shutil
from pathlib import Path
import pytest

# Use pytest-qgis to bootstrap a QGIS app and iface
pytest_plugins = ("pytest_qgis",)

# Expose tests data dir as in AcATaMa
pytest.tests_data_dir = Path(__file__).parent.resolve() / "data"


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
def thrase_dialog_stub(monkeypatch):
    """Install a lightweight stub as ThRasE.dialog for headless tests"""
    from ThRasE.thrase import ThRasE as _ThRasE
    _ThRasE.dialog = DummyDialog()
    yield _ThRasE.dialog
    # cleanup tmp dir produced by any processing
    try:
        if getattr(_ThRasE, "tmp_dir", None) and os.path.isdir(_ThRasE.tmp_dir):
            shutil.rmtree(_ThRasE.tmp_dir, ignore_errors=True)
    except Exception:
        pass
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

