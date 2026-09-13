import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from osgeo import gdal
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRasterRange,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QEventLoop, QTimer
from qgis.PyQt.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QPushButton, QWidget

from ThRasE.core.editing import LayerToEdit
from ThRasE.core.raster_recode import (
    RasterMaskSpec,
    RasterRecodeRecoveryError,
    RecodeRequest,
    RecodeStatus,
    VectorMaskSpec,
    discard_staged,
    finalize_commit,
    stage_recode,
)
from ThRasE.gui.raster_recode_task import RasterRecodeController, RasterRecodeTask
from ThRasE.utils.qgis_utils import commit_staged_and_reload, load_layer


class _MessageBar:
    def __init__(self):
        self.messages = []

    def pushMessage(self, message, **kwargs):
        self.messages.append((message, kwargs))


class _RegistryWidget:
    def __init__(self):
        self.update_count = 0

    def update_registry(self):
        self.update_count += 1


class _Owner(QWidget):
    def __init__(self):
        super().__init__()
        self.MsgBar = _MessageBar()
        self.registry_widget = _RegistryWidget()
        self.editing_status = QLabel()
        self.close_after_global_edit_cancel = False
        self.busy_states = []

    def set_global_edit_active(self, active):
        self.busy_states.append(active)


class _SourceDialog(QDialog):
    """Stand-in for the mask dialog: its own message bar plus some content widgets."""

    def __init__(self):
        super().__init__()
        self.MsgBar = _MessageBar()
        self.content = QWidget(self)
        self.already_disabled = QPushButton(self)
        self.already_disabled.setEnabled(False)


def _wait_until_inactive(controller):
    event_loop = QEventLoop()
    poll_timer = QTimer()
    poll_timer.setInterval(10)
    poll_timer.timeout.connect(lambda: event_loop.quit() if not controller.active else None)
    poll_timer.start()
    QTimer.singleShot(10_000, event_loop.quit)
    event_loop.exec()
    poll_timer.stop()


@pytest.mark.usefixtures("qgis_new_project")
def test_qgis_task_stages_commits_and_reloads(tmp_path):
    source = tmp_path / "task.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 5, 4, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).WriteArray(np.ones((4, 5), dtype=np.uint8))
    dataset = None
    layer = load_layer(str(source), name="task")
    assert layer is not None and layer.isValid()
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit

    owner = _Owner()
    controller = RasterRecodeController(owner)
    completed = []
    event_loop = QEventLoop()

    def completed_edit(result):
        completed.append(result)
        event_loop.quit()

    assert controller.start(
        RecodeRequest(str(source), 1, ((1, 7),), window_width=2, window_height=2),
        layer_to_edit,
        parent=owner,
        on_success=completed_edit,
    )
    QTimer.singleShot(10_000, event_loop.quit)
    event_loop.exec()

    assert not controller.active
    result = gdal.Open(str(source), gdal.GA_ReadOnly)
    np.testing.assert_array_equal(result.GetRasterBand(1).ReadAsArray(), np.full((4, 5), 7, dtype=np.uint8))
    result = None
    assert len(completed) == 1
    assert completed[0].status is RecodeStatus.COMMITTED
    assert completed[0].stage_path is None
    assert completed[0].lock_path is None
    assert owner.busy_states == [True, False]
    assert owner.editing_status.text() == "20 pixels edited!"
    assert not any(kwargs.get("level") == Qgis.MessageLevel.Warning for _message, kwargs in owner.MsgBar.messages)
    assert not list(tmp_path.glob(".task.tif.thrase*"))
    owner.deleteLater()


@pytest.mark.usefixtures("qgis_new_project")
def test_qgis_task_cancellation_restores_ui(tmp_path, monkeypatch):
    from ThRasE.core.raster_recode import RasterRecodeCancelled
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "cancel.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 1, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="cancel")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit

    def wait_for_cancel(_request, *, progress_callback, is_cancelled, **_kwargs):
        while not is_cancelled():
            progress_callback(25)
            time.sleep(0.005)
        raise RasterRecodeCancelled("cancelled")

    monkeypatch.setattr(raster_recode_task, "stage_recode", wait_for_cancel)
    owner = _Owner()
    controller = RasterRecodeController(owner)
    event_loop = QEventLoop()
    poll_timer = QTimer()
    poll_timer.setInterval(10)
    poll_timer.timeout.connect(lambda: event_loop.quit() if not controller.active else None)
    poll_timer.start()
    assert controller.start(RecodeRequest(str(source), 1, ((1, 2),)), layer_to_edit, parent=owner)
    QTimer.singleShot(25, controller.cancel)
    QTimer.singleShot(10_000, event_loop.quit)
    event_loop.exec()
    poll_timer.stop()

    assert not controller.active
    assert owner.busy_states == [True, False]
    assert any("cancelled" in message.lower() for message, _kwargs in owner.MsgBar.messages)
    owner.deleteLater()


@pytest.mark.usefixtures("qgis_new_project")
def test_commit_releases_qgis_provider_and_restores_layer_state(tmp_path, monkeypatch, qgis_iface):
    from ThRasE.core import raster_recode

    source = tmp_path / "release.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 2, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).WriteArray(np.array([[1, 2]], dtype=np.uint8))
    dataset = None
    layer = load_layer(str(source), name="release")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    renderer_type = layer.renderer().type()
    provider = layer.dataProvider()
    provider.setUserNoDataValue(1, [QgsRasterRange(7, 7)])
    provider.setUseSourceNoDataValue(1, False)
    provider.setZoomedInResamplingMethod(provider.ResamplingMethod.Bilinear)
    provider.setZoomedOutResamplingMethod(provider.ResamplingMethod.Cubic)
    provider.enableProviderResampling(True)
    provider.setDpi(333)
    canvas = qgis_iface.mapCanvas()
    canvas_was_frozen = canvas.isFrozen()
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 7),)))
    original_commit = raster_recode.commit_staged

    def checked_commit(staged_result):
        assert not layer.isValid()
        assert layer_to_edit.data_provider is None
        assert canvas.isFrozen()
        return original_commit(staged_result)

    monkeypatch.setattr(raster_recode, "commit_staged", checked_commit)
    receipt, restored = commit_staged_and_reload(result, additional_layers=(layer,))
    assert finalize_commit(receipt) == ()

    assert restored == [layer]
    assert layer.isValid()
    assert layer.renderer().type() == renderer_type
    assert layer_to_edit.data_provider is layer.dataProvider()
    assert not layer.dataProvider().useSourceNoDataValue(1)
    nodata_ranges = layer.dataProvider().userNoDataValues(1)
    assert len(nodata_ranges) == 1 and nodata_ranges[0].min() == 7 and nodata_ranges[0].max() == 7
    assert layer.dataProvider().isProviderResamplingEnabled()
    assert layer.dataProvider().zoomedInResamplingMethod() == provider.ResamplingMethod.Bilinear
    assert layer.dataProvider().zoomedOutResamplingMethod() == provider.ResamplingMethod.Cubic
    assert layer.dataProvider().dpi() == 333
    assert canvas.isFrozen() == canvas_was_frozen
    dataset = gdal.Open(str(source), gdal.GA_ReadOnly)
    np.testing.assert_array_equal(dataset.GetRasterBand(1).ReadAsArray(), [[7, 2]])
    dataset = None


@pytest.mark.usefixtures("qgis_new_project")
def test_commit_releases_matching_layer_held_only_by_custom_canvas(tmp_path, qgis_iface):
    from ThRasE.utils.qgis_utils import release_raster_layers_for_source, restore_released_raster_layers

    source = tmp_path / "custom-canvas.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 1, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = QgsRasterLayer(str(source), "custom canvas", "gdal")
    assert layer.isValid()
    assert layer.id() not in QgsProject.instance().mapLayers()
    canvas = QgsMapCanvas()
    canvas.setLayers([layer])

    resources = release_raster_layers_for_source(str(source))
    try:
        assert [state.layer for state in resources.layers] == [layer]
        assert not layer.isValid()
    finally:
        restore_released_raster_layers(resources)
        canvas.deleteLater()
    assert layer.isValid()


@pytest.mark.usefixtures("qgis_new_project")
def test_reload_failure_rolls_back_raster_and_reconnects_layer(tmp_path, monkeypatch):
    from ThRasE.utils import qgis_utils

    source = tmp_path / "rollback.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 2, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).WriteArray(np.array([[1, 2]], dtype=np.uint8))
    dataset = None
    layer = load_layer(str(source), name="rollback")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    result = stage_recode(RecodeRequest(str(source), 1, ((1, 7),)))
    original_restore = qgis_utils.restore_released_raster_layers
    restore_calls = 0

    def fail_first_restore(states):
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 1:
            raise RuntimeError("simulated reload failure")
        return original_restore(states)

    monkeypatch.setattr(qgis_utils, "restore_released_raster_layers", fail_first_restore)
    with pytest.raises(Exception, match="original raster was restored"):
        commit_staged_and_reload(result, additional_layers=(layer,))

    dataset = gdal.Open(str(source), gdal.GA_ReadOnly)
    np.testing.assert_array_equal(dataset.GetRasterBand(1).ReadAsArray(), [[1, 2]])
    dataset = None
    assert layer.isValid()
    assert layer_to_edit.data_provider is layer.dataProvider()
    assert not list(Path(tmp_path).glob(".rollback.tif.thrase*"))


@pytest.mark.usefixtures("qgis_new_project")
@pytest.mark.parametrize("pairs, callback_name", [(((1, 7),), "on_success"), (((9, 7),), "on_no_changes")])
def test_completion_callback_failure_always_restores_controller(tmp_path, pairs, callback_name):
    source = tmp_path / f"callback-{callback_name}.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 1, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name=callback_name)
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    owner = _Owner()
    controller = RasterRecodeController(owner)

    def fail_callback(_result):
        raise RuntimeError("simulated callback failure")

    callback = {callback_name: fail_callback}
    assert controller.start(RecodeRequest(str(source), 1, pairs), layer_to_edit, parent=owner, **callback)
    _wait_until_inactive(controller)

    assert not controller.active
    assert owner.busy_states == [True, False]
    assert any("completion action failed" in message for message, _kwargs in owner.MsgBar.messages)
    owner.deleteLater()


@pytest.mark.usefixtures("qgis_new_project")
def test_startup_exception_does_not_leave_controller_busy(tmp_path, monkeypatch):
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "startup.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 1, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="startup")
    layer_to_edit = LayerToEdit(layer, 1)
    mask_layer = QgsVectorLayer("Polygon", "mask", "memory")

    def fail_feature_source(_layer):
        raise RuntimeError("simulated feature-source failure")

    monkeypatch.setattr(raster_recode_task, "QgsVectorLayerFeatureSource", fail_feature_source)
    owner = _Owner()
    controller = RasterRecodeController(owner)
    assert not controller.start(
        RecodeRequest(str(source), 1, ((1, 2),), mask=VectorMaskSpec()),
        layer_to_edit,
        parent=owner,
        vector_mask_layer=mask_layer,
    )

    assert not controller.active
    assert controller.progress is None
    assert owner.busy_states == []
    owner.deleteLater()


@pytest.mark.usefixtures("qgis_new_project")
def test_recovery_error_keeps_backup_when_controller_cleans_up(tmp_path, monkeypatch):
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "recovery.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 1, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="recovery")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    request = RecodeRequest(str(source), 1, ((1, 2),))
    result = stage_recode(request)
    assert result.stage_path is not None and result.lock_path is not None

    def fail_commit(*_args, **_kwargs):
        raise RasterRecodeRecoveryError("manual recovery required")

    monkeypatch.setattr(raster_recode_task, "commit_and_reconcile", fail_commit)
    owner = _Owner()
    controller = RasterRecodeController(owner)
    task = RasterRecodeTask(request, controller)
    task.result = result
    controller.task = task
    controller.layer_to_edit = layer_to_edit
    controller.task_finished(task, True)

    assert Path(result.stage_path).exists()
    assert Path(result.lock_path).exists()
    assert not controller.active
    assert discard_staged(result) == ()
    owner.deleteLater()


def test_qgis_commit_wrapper_preserves_recovery_error_class(monkeypatch):
    from ThRasE.core import raster_recode
    from ThRasE.utils import qgis_utils

    resources = object()
    monkeypatch.setattr(qgis_utils, "release_raster_layers_for_source", lambda *_args: resources)
    monkeypatch.setattr(
        raster_recode,
        "commit_staged",
        lambda _result: (_ for _ in ()).throw(RasterRecodeRecoveryError("rollback failed")),
    )
    monkeypatch.setattr(
        qgis_utils,
        "restore_released_raster_layers",
        lambda _resources: (_ for _ in ()).throw(RuntimeError("reconnect failed")),
    )

    with pytest.raises(RasterRecodeRecoveryError, match="could not reconnect"):
        commit_staged_and_reload(SimpleNamespace(source_path="unused"))


@pytest.mark.usefixtures("qgis_new_project")
def test_controller_shutdown_cancels_and_waits_for_worker(tmp_path, monkeypatch):
    from ThRasE.core.raster_recode import RasterRecodeCancelled
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "shutdown.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 1, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="shutdown")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit

    def wait_for_cancel(_request, *, progress_callback, is_cancelled, **_kwargs):
        while not is_cancelled():
            progress_callback(25)
            time.sleep(0.005)
        raise RasterRecodeCancelled("cancelled")

    monkeypatch.setattr(raster_recode_task, "stage_recode", wait_for_cancel)
    owner = _Owner()
    controller = RasterRecodeController(owner)
    assert controller.start(RecodeRequest(str(source), 1, ((1, 2),)), layer_to_edit, parent=owner)
    assert controller.shutdown()

    assert not controller.active
    assert owner.busy_states == [True, False]
    owner.deleteLater()


def test_canvas_stop_timeout_restores_original_freeze_state(monkeypatch):
    from ThRasE.utils import qgis_utils

    class Canvas:
        def __init__(self):
            self.frozen = False

        def isFrozen(self):
            return self.frozen

        def freeze(self, frozen):
            self.frozen = frozen

        def stopRendering(self):
            pass

        def isDrawing(self):
            return True

    canvas = Canvas()
    monkeypatch.setattr(qgis_utils.iface, "mapCanvas", lambda: canvas)
    monkeypatch.setattr(qgis_utils.QApplication, "allWidgets", lambda: [])

    with pytest.raises(RuntimeError, match="Timed out"):
        qgis_utils._freeze_raster_canvases(timeout_ms=0)
    assert not canvas.frozen


def test_file_transaction_propagates_operation_timeout_error():
    from ThRasE.utils.qgis_utils import _run_file_transaction

    def fail_with_timeout():
        raise TimeoutError("operation timeout")

    with pytest.raises(TimeoutError, match="operation timeout"):
        _run_file_transaction(fail_with_timeout)


def test_finalization_uses_file_transaction_worker(monkeypatch):
    from ThRasE.utils import qgis_utils

    calls = []

    def capture(operation, *args):
        calls.append((operation.__name__, args))
        return ("retained",)

    monkeypatch.setattr(qgis_utils, "_run_file_transaction", capture)
    receipt = object()
    assert qgis_utils.finalize_commit_off_thread(receipt) == ("retained",)
    assert calls == [("finalize_commit", (receipt,))]


@pytest.mark.usefixtures("qgis_new_project")
def test_partial_progress_dialog_startup_is_cleaned_up(tmp_path, monkeypatch):
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "progress-startup.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 1, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="progress startup")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    cleanup = []

    class FailingProgressDialog:
        def __init__(self, *_args):
            pass

        def setWindowTitle(self, _title):
            raise RuntimeError("simulated progress setup failure")

        def close(self):
            cleanup.append("closed")

        def deleteLater(self):
            cleanup.append("deleted")

    monkeypatch.setattr(raster_recode_task, "QProgressDialog", FailingProgressDialog)
    owner = _Owner()
    controller = RasterRecodeController(owner)
    assert not controller.start(RecodeRequest(str(source), 1, ((1, 2),)), layer_to_edit, parent=owner)
    assert cleanup == ["closed", "deleted"]
    assert not controller.active
    assert owner.busy_states == []
    owner.deleteLater()


@pytest.mark.usefixtures("qgis_new_project")
def test_raster_mask_layer_is_registered_as_task_dependency(tmp_path, monkeypatch):
    source = tmp_path / "dependency-source.tif"
    mask = tmp_path / "dependency-mask.tif"
    for path in (source, mask):
        dataset = gdal.GetDriverByName("GTiff").Create(str(path), 1, 1, 1, gdal.GDT_Byte)
        dataset.GetRasterBand(1).Fill(1)
        dataset = None
    source_layer = load_layer(str(source), name="dependency source")
    mask_layer = load_layer(str(mask), name="dependency mask")
    layer_to_edit = LayerToEdit(source_layer, 1)
    LayerToEdit.current = layer_to_edit
    scheduled_tasks = []

    class RejectingTaskManager:
        def addTask(self, task):
            scheduled_tasks.append(task)
            return False

    monkeypatch.setattr(QgsApplication, "taskManager", lambda: RejectingTaskManager())
    owner = _Owner()
    controller = RasterRecodeController(owner)
    assert not controller.start(
        RecodeRequest(str(source), 1, ((1, 2),), mask=RasterMaskSpec(str(mask), 1, (1,))),
        layer_to_edit,
        parent=owner,
    )
    dependent_ids = {layer.id() for layer in scheduled_tasks[0].dependentLayers()}
    assert dependent_ids == {source_layer.id(), mask_layer.id()}
    owner.deleteLater()


@pytest.mark.usefixtures("qgis_new_project")
def test_task_streams_uncommitted_memory_vector_mask_to_disk(tmp_path, monkeypatch):
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "memory-mask.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 4, 4, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, 4, 0, -1))
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="target")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit

    mask_layer = QgsVectorLayer("Polygon", "memory mask", "memory")
    QgsProject.instance().addMapLayer(mask_layer)
    mask_layer.startEditing()
    feature = QgsFeature(mask_layer.fields())
    feature.setGeometry(
        QgsGeometry.fromPolygonXY(
            [[QgsPointXY(0, 4), QgsPointXY(2, 4), QgsPointXY(2, 2), QgsPointXY(0, 2), QgsPointXY(0, 4)]]
        )
    )
    assert mask_layer.addFeature(feature)

    snapshot_permissions = []
    original_delete_snapshot = raster_recode_task._delete_vector_snapshot

    def inspect_snapshot_permissions(path):
        if path and Path(path).exists():
            snapshot_permissions.append((Path(path).stat().st_mode & 0o777, Path(path).parent.stat().st_mode & 0o777))
        return original_delete_snapshot(path)

    monkeypatch.setattr(raster_recode_task, "_delete_vector_snapshot", inspect_snapshot_permissions)

    owner = _Owner()
    controller = RasterRecodeController(owner)
    completed = []
    assert controller.start(
        RecodeRequest(str(source), 1, ((1, 8),), mask=VectorMaskSpec()),
        layer_to_edit,
        parent=owner,
        vector_mask_layer=mask_layer,
        on_success=completed.append,
    )
    _wait_until_inactive(controller)

    dataset = gdal.Open(str(source), gdal.GA_ReadOnly)
    expected = np.ones((4, 4), dtype=np.uint8)
    expected[:2, :2] = 8
    np.testing.assert_array_equal(dataset.GetRasterBand(1).ReadAsArray(), expected)
    dataset = None
    assert len(completed) == 1
    assert snapshot_permissions == [(0o600, 0o700)]
    assert not list(tmp_path.glob(".thrase-vector-mask-*"))
    owner.deleteLater()


def test_plugin_unload_drains_raster_task_before_removing_actions():
    from ThRasE.thrase import ThRasE

    events = []

    class Controller:
        def shutdown(self):
            events.append("shutdown")
            return True

    class Dialog:
        def __init__(self):
            self.raster_recode_controller = Controller()
            self.closing_for_unload = False

        def close(self):
            assert self.closing_for_unload
            events.append("close")

    class Interface:
        def removePluginMenu(self, _menu, _action):
            events.append("menu")

        def removeToolBarIcon(self, _action):
            events.append("toolbar")

    plugin = ThRasE.__new__(ThRasE)
    plugin.iface = Interface()
    plugin.menu_name_plugin = "ThRasE"
    plugin.dockable_action = object()
    plugin.about_action = object()
    previous_dialog = ThRasE.dialog
    try:
        ThRasE.dialog = Dialog()
        plugin.unload()
    finally:
        ThRasE.dialog = previous_dialog

    assert events == ["shutdown", "close", "menu", "menu", "toolbar"]


def test_plugin_unload_raises_when_task_cannot_be_drained():
    from ThRasE.thrase import ThRasE

    events = []

    class Controller:
        def shutdown(self):
            events.append("shutdown")
            return False

    class Dialog:
        raster_recode_controller = Controller()

    class Interface:
        def removePluginMenu(self, _menu, _action):
            events.append("menu")

        def removeToolBarIcon(self, _action):
            events.append("toolbar")

    plugin = ThRasE.__new__(ThRasE)
    plugin.iface = Interface()
    plugin.menu_name_plugin = "ThRasE"
    plugin.dockable_action = object()
    plugin.about_action = object()
    previous_dialog = ThRasE.dialog
    try:
        ThRasE.dialog = Dialog()
        with pytest.raises(RuntimeError, match="cannot be unloaded"):
            plugin.unload()
    finally:
        ThRasE.dialog = previous_dialog

    assert events == ["shutdown"]


@pytest.mark.usefixtures("qgis_new_project")
def test_progress_dialog_stays_usable_while_the_source_dialog_is_disabled(tmp_path, monkeypatch):
    from ThRasE.core.raster_recode import RasterRecodeCancelled
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "cancel-from-dialog.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(source), 1, 1, 1, gdal.GDT_Byte)
    dataset.GetRasterBand(1).Fill(1)
    dataset = None
    layer = load_layer(str(source), name="cancel from dialog")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit

    def wait_for_cancel(_request, *, progress_callback, is_cancelled, **_kwargs):
        while not is_cancelled():
            progress_callback(25)
            time.sleep(0.005)
        raise RasterRecodeCancelled("cancelled")

    monkeypatch.setattr(raster_recode_task, "stage_recode", wait_for_cancel)
    owner = _Owner()
    source_dialog = _SourceDialog()
    controller = RasterRecodeController(owner)
    assert controller.start(
        RecodeRequest(str(source), 1, ((1, 2),)),
        layer_to_edit,
        parent=source_dialog,
        source_dialog=source_dialog,
    )

    # the dialog can no longer be used to change the request being edited...
    assert not source_dialog.content.isEnabled()
    # ...but its progress dialog, which is parented to it, must still accept Cancel
    assert controller.progress.isEnabled()
    cancel_button = controller.progress.findChild(QPushButton)
    assert cancel_button is not None and cancel_button.isEnabled()

    controller.cancel()
    _wait_until_inactive(controller)

    assert not controller.active
    assert source_dialog.content.isEnabled()
    # a widget the dialog had already disabled itself stays disabled
    assert not source_dialog.already_disabled.isEnabled()
    source_dialog.deleteLater()
    owner.deleteLater()


def test_errors_reach_the_message_bar_of_the_dialog_that_started_the_edit():
    class UnreadableRegistry:
        def __iter__(self):
            raise RuntimeError("simulated registry failure")

    owner = _Owner()
    source_dialog = _SourceDialog()
    controller = RasterRecodeController(owner)
    layer_to_edit = SimpleNamespace(pixel_log_store=UnreadableRegistry())

    assert not controller.start(
        RecodeRequest("unused.tif", 1, ((1, 2),)),
        layer_to_edit,
        parent=source_dialog,
        source_dialog=source_dialog,
    )

    # the modal dialog covers the main window, so its own bar is the visible one
    assert any("simulated registry failure" in message for message, _kwargs in source_dialog.MsgBar.messages)
    assert owner.MsgBar.messages == []
    source_dialog.deleteLater()
    owner.deleteLater()


def test_shutdown_waits_out_the_commit_phase():
    owner = _Owner()
    controller = RasterRecodeController(owner)
    controller.committing = True
    QTimer.singleShot(20, lambda: setattr(controller, "committing", False))

    assert controller.shutdown(timeout_ms=5_000)
    owner.deleteLater()


def test_shutdown_gives_up_when_the_commit_phase_does_not_end():
    owner = _Owner()
    controller = RasterRecodeController(owner)
    controller.committing = True

    assert not controller.shutdown(timeout_ms=50)
    owner.deleteLater()


def test_worker_reports_only_the_messages_logged_by_its_own_thread():
    import logging
    import threading

    from ThRasE.core.raster_recode import GLOBAL_EDIT_LOGGER_NAME
    from ThRasE.gui.raster_recode_task import _collect_worker_messages

    logger = logging.getLogger(GLOBAL_EDIT_LOGGER_NAME)
    logged_elsewhere = threading.Event()

    def log_from_another_thread():
        logger.warning("a message from an unrelated thread")
        logged_elsewhere.set()

    with _collect_worker_messages() as collector:
        logger.warning("a message from the worker thread")
        thread = threading.Thread(target=log_from_another_thread)
        thread.start()
        thread.join()

    assert logged_elsewhere.is_set()
    assert collector.messages == ["a message from the worker thread"]


def _create_ones_raster(path, columns=5, rows=4):
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), columns, rows, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, rows, 0, -1))
    dataset.GetRasterBand(1).WriteArray(np.ones((rows, columns), dtype=np.uint8))
    dataset = None


def _read_band(path):
    dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
    try:
        return dataset.GetRasterBand(1).ReadAsArray()
    finally:
        dataset = None


@pytest.mark.usefixtures("qgis_new_project")
@pytest.mark.parametrize(
    "reply, edited",
    [(QMessageBox.StandardButton.Ok, True), (QMessageBox.StandardButton.Cancel, False)],
    ids=["continue", "cancel"],
)
def test_large_registry_addition_is_confirmed_before_the_raster_is_copied(tmp_path, monkeypatch, reply, edited):
    from ThRasE.core import raster_recode
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "large-registry.tif"
    _create_ones_raster(source)
    layer = load_layer(str(source), name="large registry")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    questions = []

    def answer(_parent, title, text, *_args, **_kwargs):
        questions.append((title, text))
        return reply

    monkeypatch.setattr(raster_recode_task.QMessageBox, "warning", answer)
    monkeypatch.setattr(raster_recode_task, "WARNING_REGISTRY_LIMIT", 10)
    copies = []
    original_copy = raster_recode._copy_dataset_files

    def counted_copy(driver, destination, copied_source):
        copies.append(destination)
        return original_copy(driver, destination, copied_source)

    monkeypatch.setattr(raster_recode, "_copy_dataset_files", counted_copy)
    owner = _Owner()
    controller = RasterRecodeController(owner)
    assert controller.start(
        RecodeRequest(str(source), 1, ((1, 7),), collect_changes=True),
        layer_to_edit,
        parent=owner,
    )
    _wait_until_inactive(controller)

    assert not controller.active
    assert len(questions) == 1
    # the count is shown; the threshold is an internal guard and never is
    assert "20 pixels" in questions[0][1] and "10" not in questions[0][1]
    if edited:
        # continuing records every changed pixel: the warning never trims the registry
        np.testing.assert_array_equal(_read_band(source), np.full((4, 5), 7, dtype=np.uint8))
        assert len(layer_to_edit.pixel_log_store) == 20
        assert len(copies) == 1
    else:
        # the question is asked before any of the expensive work: the raster was never copied
        np.testing.assert_array_equal(_read_band(source), np.ones((4, 5), dtype=np.uint8))
        assert layer_to_edit.pixel_log_store == {}
        assert copies == []
        assert any("cancelled" in message.lower() for message, _kwargs in owner.MsgBar.messages)
    assert not list(tmp_path.glob(".large-registry.tif.thrase*"))
    owner.deleteLater()


@pytest.mark.usefixtures("qgis_new_project")
def test_registry_warning_is_skipped_when_changes_are_not_recorded(tmp_path, monkeypatch):
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "unrecorded.tif"
    _create_ones_raster(source)
    layer = load_layer(str(source), name="unrecorded")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit

    def unexpected_question(*_args, **_kwargs):
        raise AssertionError("no registry warning is expected when changes are not recorded")

    monkeypatch.setattr(raster_recode_task.QMessageBox, "warning", unexpected_question)
    monkeypatch.setattr(raster_recode_task, "WARNING_REGISTRY_LIMIT", 1)
    owner = _Owner()
    controller = RasterRecodeController(owner)
    assert controller.start(
        RecodeRequest(str(source), 1, ((1, 7),)),
        layer_to_edit,
        parent=owner,
    )
    _wait_until_inactive(controller)

    np.testing.assert_array_equal(_read_band(source), np.full((4, 5), 7, dtype=np.uint8))
    assert layer_to_edit.pixel_log_store == {}
    owner.deleteLater()


@pytest.mark.usefixtures("qgis_new_project")
def test_progress_dialog_names_the_step_that_is_running(tmp_path, monkeypatch):
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "steps.tif"
    _create_ones_raster(source)
    layer = load_layer(str(source), name="steps")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    labels = []
    original_set_label = raster_recode_task.QProgressDialog.setLabelText

    def record_label(dialog, text):
        labels.append(text)
        original_set_label(dialog, text)

    monkeypatch.setattr(raster_recode_task.QProgressDialog, "setLabelText", record_label)
    owner = _Owner()
    controller = RasterRecodeController(owner)
    assert controller.start(RecodeRequest(str(source), 1, ((1, 7),)), layer_to_edit, parent=owner)
    _wait_until_inactive(controller)

    # the raster is only a working copy until the last step, and the label says so
    assert labels == [
        "Copying the raster to a working copy...",
        "Applying the changes to the working copy...",
        "Verifying the changes...",
        "Replacing the raster and updating the registry...",
    ]
    np.testing.assert_array_equal(_read_band(source), np.full((4, 5), 7, dtype=np.uint8))
    owner.deleteLater()


@pytest.mark.usefixtures("qgis_new_project")
@pytest.mark.parametrize("button, edited", [("Ok", True), ("Cancel", False)], ids=["continue", "cancel"])
def test_registry_warning_is_a_real_dialog_shown_over_the_progress_dialog(tmp_path, monkeypatch, button, edited):
    """Unlike the other warning tests, this one lets the real QMessageBox open and answers it through Qt."""
    from ThRasE.gui import raster_recode_task

    source = tmp_path / "real-warning.tif"
    _create_ones_raster(source)
    layer = load_layer(str(source), name="real warning")
    layer_to_edit = LayerToEdit(layer, 1)
    LayerToEdit.current = layer_to_edit
    monkeypatch.setattr(raster_recode_task, "WARNING_REGISTRY_LIMIT", 10)
    seen = []
    polls = [0]

    def answer_when_shown():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, QMessageBox) and "will be added to the registry" in dialog.text():
            seen.append((dialog.windowTitle(), dialog.isVisible()))
            dialog.button(getattr(QMessageBox.StandardButton, button)).click()
            return
        polls[0] += 1
        if polls[0] < 500:
            QTimer.singleShot(20, answer_when_shown)

    QTimer.singleShot(20, answer_when_shown)
    owner = _Owner()
    controller = RasterRecodeController(owner)
    assert controller.start(RecodeRequest(str(source), 1, ((1, 7),), collect_changes=True), layer_to_edit, parent=owner)
    _wait_until_inactive(controller)

    assert seen == [("ThRasE registry", True)]
    if edited:
        np.testing.assert_array_equal(_read_band(source), np.full((4, 5), 7, dtype=np.uint8))
        assert len(layer_to_edit.pixel_log_store) == 20
    else:
        np.testing.assert_array_equal(_read_band(source), np.ones((4, 5), dtype=np.uint8))
        assert layer_to_edit.pixel_log_store == {}
    owner.deleteLater()


class _StoppableController:
    """Stand-in for the raster controller: only the state a close decision reads."""

    def __init__(self, *, active=True, committing=False):
        self.active = active
        self.committing = committing
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1


def _closing_dialog(controller):
    """Build a stand-in for the main dialog carrying its real close decision."""
    from ThRasE.gui import main_dialog

    class _ClosingDialog:
        _ready_to_close = main_dialog.ThRasEDialog._ready_to_close
        _forget_deferred_close = main_dialog.ThRasEDialog._forget_deferred_close

        def __init__(self):
            self.raster_recode_controller = controller
            self.close_confirmed = False
            self.close_after_global_edit_cancel = False
            self.save_prompts = 0

        def tr(self, message):
            return message

        def _confirm_close(self):
            self.save_prompts += 1
            return True

    return _ClosingDialog()


def _stub_close_prompts(monkeypatch, *, answer=None, while_asking=None):
    """Answer the close prompts of the main dialog and record their titles."""
    from ThRasE.gui import main_dialog

    asked = []

    class _Prompts:
        StandardButton = QMessageBox.StandardButton

        @staticmethod
        def question(_parent, title, _text, *_args, **_kwargs):
            asked.append(title)
            if while_asking is not None:
                while_asking()
            return answer

        @staticmethod
        def information(_parent, title, _text, *_args, **_kwargs):
            asked.append(title)

    monkeypatch.setattr(main_dialog, "QMessageBox", _Prompts)
    return asked


def test_cancelling_a_global_edit_to_close_asks_to_save_exactly_once(monkeypatch):
    controller = _StoppableController()
    asked = _stub_close_prompts(monkeypatch, answer=QMessageBox.StandardButton.Yes)
    dialog = _closing_dialog(controller)

    # the edit is still running: closing waits for the worker, after asking about saving
    assert dialog._ready_to_close() is False
    assert controller.cancel_calls == 1
    assert dialog.close_confirmed and dialog.close_after_global_edit_cancel
    assert dialog.save_prompts == 1

    # the worker stopped and the controller closes the dialog: the answer is not asked again
    controller.active = False
    assert dialog._ready_to_close() is True
    assert dialog.save_prompts == 1
    assert not dialog.close_confirmed and not dialog.close_after_global_edit_cancel

    # a later close is a new decision, so it asks again instead of reusing the old answer
    assert dialog._ready_to_close() is True
    assert dialog.save_prompts == 2
    assert asked == ["Cancel global edit"]


def test_close_is_not_deferred_when_the_global_edit_ends_while_a_prompt_is_open(monkeypatch):
    """Both prompts run their own event loop, so the edit can finish inside one of them."""
    controller = _StoppableController()
    asked = _stub_close_prompts(
        monkeypatch,
        answer=QMessageBox.StandardButton.Yes,
        while_asking=lambda: setattr(controller, "active", False),
    )
    dialog = _closing_dialog(controller)

    # nothing is left to wait for, so the confirmed close happens now
    assert dialog._ready_to_close() is True
    assert controller.cancel_calls == 0
    assert dialog.save_prompts == 1
    # a deferred close left behind here would close a later edit without asking to save
    assert not dialog.close_confirmed and not dialog.close_after_global_edit_cancel
    assert asked == ["Cancel global edit"]


def test_close_is_refused_while_the_global_edit_is_being_finalized(monkeypatch):
    controller = _StoppableController(committing=True)
    asked = _stub_close_prompts(monkeypatch)
    dialog = _closing_dialog(controller)

    assert dialog._ready_to_close() is False
    assert controller.cancel_calls == 0
    assert dialog.save_prompts == 0
    assert not dialog.close_confirmed and not dialog.close_after_global_edit_cancel
    assert asked == ["Global edit"]
