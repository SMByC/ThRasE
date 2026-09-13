"""QGIS task integration for bounded raster recoding.

The GDAL engine in :mod:`ThRasE.core.raster_recode` runs in a task-manager worker
thread.  Everything that touches QGIS state stays on the main thread here:
snapshotting a live vector mask, releasing providers, replacing the raster, and
reporting the outcome.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from osgeo import ogr, osr
from qgis.core import Qgis, QgsApplication, QgsFeatureRequest, QgsProject, QgsTask, QgsVectorLayerFeatureSource
from qgis.PyQt.QtCore import QEventLoop, QObject, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import QApplication, QDialog, QMessageBox, QProgressDialog, QWidget

from ThRasE.core.raster_recode import (
    GLOBAL_EDIT_LOGGER_NAME,
    RasterMaskSpec,
    RasterRecodeCancelled,
    RasterRecodeRecoveryError,
    RecodePhase,
    RecodeRequest,
    RecodeResult,
    RecodeStatus,
    VectorMaskSpec,
    count_recode_changes,
    discard_staged,
    stage_recode,
)
from ThRasE.core.registry import WARNING_REGISTRY_LIMIT
from ThRasE.utils.qgis_utils import commit_and_reconcile, get_source_from

CompletionCallback = Callable[[RecodeResult], None]

#: What the progress dialog says while each step of the staging runs.  The raster
#: on disk is untouched during all of them; it is replaced only afterwards.
_PHASE_LABELS = {
    RecodePhase.COPYING: "Copying the raster to a working copy...",
    RecodePhase.RECODING: "Applying the changes to the working copy...",
    RecodePhase.VERIFYING: "Verifying the changes...",
}


def _ignore_label(_label: str) -> None:
    return None


def _progress_slice(progress_callback, start: float, end: float):
    """Map the 0-100 progress of one step onto the ``start``-``end`` part of the whole edit."""
    return lambda value: progress_callback(start + value * (end - start) / 100.0)


_LOGGER = logging.getLogger(GLOBAL_EDIT_LOGGER_NAME)


class _WorkerLogCollector(logging.Handler):
    """Collect global-edit log records emitted by the thread that installed it.

    The engine logs cleanup problems instead of raising them, because they must
    not abort an edit that already succeeded.  Filtering by thread keeps a message
    logged on the GUI thread from being reported as a warning of this edit.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self._thread_id = threading.get_ident()
        self.messages: list[str] = []

    def emit(self, record):
        if record.thread == self._thread_id:
            self.messages.append(record.getMessage())


@contextmanager
def _collect_worker_messages():
    logger = logging.getLogger(GLOBAL_EDIT_LOGGER_NAME)
    collector = _WorkerLogCollector()
    logger.addHandler(collector)
    try:
        yield collector
    finally:
        logger.removeHandler(collector)


def _delete_vector_snapshot(path: str | None) -> tuple[str, ...]:
    """Delete a temporary vector-mask snapshot and return whatever survived."""
    if not path:
        return ()
    deleted = not os.path.exists(path)
    driver = ogr.GetDriverByName("GPKG") if not deleted else None
    if driver is not None:
        try:
            if driver.DeleteDataSource(path) == ogr.OGRERR_NONE:
                deleted = True
        except RuntimeError:
            pass
    if not deleted:
        try:
            os.remove(path)
            deleted = True
        except OSError as error:
            _LOGGER.warning('Unable to remove the temporary vector-mask snapshot "%s": %s', path, error)
    parent = Path(path).parent
    retained = [] if deleted and not os.path.exists(path) else [path]
    if parent.name.startswith(".thrase-vector-mask-"):
        try:
            parent.rmdir()
        except OSError as error:
            _LOGGER.warning('Unable to remove the temporary vector-mask directory "%s": %s', parent, error)
        if parent.exists():
            retained.append(str(parent))
    return tuple(retained)


def _materialize_vector_mask(feature_source, crs_wkt, reference_path, progress_callback, is_cancelled) -> str:
    """Stream a QGIS feature-source snapshot to an indexed, temporary GeoPackage.

    Streaming keeps memory bounded and captures the features the layer exposes
    right now, including uncommitted edits and active filters.
    """
    snapshot_directory = tempfile.mkdtemp(prefix=".thrase-vector-mask-", dir=os.path.dirname(reference_path))
    path = str(Path(snapshot_directory, "mask.gpkg"))
    dataset = None
    layer = None
    transaction_open = False
    try:
        os.chmod(snapshot_directory, 0o700)
        driver = ogr.GetDriverByName("GPKG")
        if driver is None:
            raise RuntimeError("The GDAL GeoPackage driver is unavailable")
        dataset = driver.CreateDataSource(path)
        if dataset is None:
            raise RuntimeError("Unable to create a temporary vector-mask snapshot")
        os.chmod(path, 0o600)
        spatial_ref = None
        if crs_wkt:
            spatial_ref = osr.SpatialReference()
            if spatial_ref.ImportFromWkt(crs_wkt) != 0:
                raise RuntimeError("The vector mask has an invalid coordinate system")
        layer = dataset.CreateLayer("mask", spatial_ref, ogr.wkbUnknown, options=["SPATIAL_INDEX=YES"])
        if layer is None:
            raise RuntimeError("Unable to create the temporary vector-mask layer")
        definition = layer.GetLayerDefn()
        transaction_open = layer.StartTransaction() == ogr.OGRERR_NONE
        written = 0
        for index, source_feature in enumerate(
            feature_source.getFeatures(QgsFeatureRequest().setNoAttributes()), start=1
        ):
            if is_cancelled():
                raise RasterRecodeCancelled("Raster recoding was cancelled")
            geometry = source_feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            ogr_geometry = ogr.CreateGeometryFromWkb(bytes(geometry.asWkb()))
            if ogr_geometry is None:
                continue
            feature = ogr.Feature(definition)
            feature.SetGeometry(ogr_geometry)
            if layer.CreateFeature(feature) != ogr.OGRERR_NONE:
                raise RuntimeError("Unable to write a vector-mask feature to the temporary snapshot")
            written += 1
            if transaction_open and written % 1000 == 0:
                if layer.CommitTransaction() != ogr.OGRERR_NONE or layer.StartTransaction() != ogr.OGRERR_NONE:
                    raise RuntimeError("Unable to commit the temporary vector-mask snapshot")
            if index % 256 == 0:
                progress_callback(min(9.0, 1.0 + index / 1000.0))
        if transaction_open:
            if layer.CommitTransaction() != ogr.OGRERR_NONE:
                raise RuntimeError("Unable to commit the temporary vector-mask snapshot")
            transaction_open = False
        if written == 0:
            raise RuntimeError("The selected vector mask has no polygon features")
        progress_callback(10.0)
        layer = None
        close = getattr(dataset, "Close", None)
        if close is not None and close() not in (None, ogr.OGRERR_NONE):
            raise RuntimeError("Unable to finalize the temporary vector-mask snapshot")
        dataset = None
        return path
    except Exception as error:
        if transaction_open and layer is not None:
            layer.RollbackTransaction()
        layer = None
        dataset = None
        retained = _delete_vector_snapshot(path)
        if retained:
            raise RuntimeError(f"{error}; temporary vector-mask files remain at: " + ", ".join(retained)) from error
        raise


def stage_recode_with_qgis_mask(
    request: RecodeRequest,
    vector_mask_source=None,
    *,
    progress_callback,
    is_cancelled,
    report_phase: Callable[[str], None] = _ignore_label,
    confirm_registry_addition: Callable[[int], bool] | None = None,
) -> RecodeResult:
    """Materialize live QGIS vector state in bounded storage, then run the pure GDAL worker.

    ``report_phase`` receives a short description of each step as it starts.

    When the request records its changes and ``confirm_registry_addition`` is
    given, the pixels the edit would change are counted first with one read of
    the source, and above ``WARNING_REGISTRY_LIMIT`` the callback must return
    True before the raster is copied and recoded.  A False answer cancels the
    edit while nothing has been done yet.
    """

    def report_engine_phase(phase: RecodePhase) -> None:
        report_phase(_PHASE_LABELS[phase])

    snapshot_path = None
    done = 0.0  # share of the whole edit's progress already reported
    if vector_mask_source is not None:
        if not isinstance(request.mask, VectorMaskSpec):
            raise RuntimeError("A vector-mask feature source requires a vector mask request")
        report_phase("Reading the vector mask...")
        # reports its own 0-10 % of the whole edit
        snapshot_path = _materialize_vector_mask(
            vector_mask_source, request.mask.crs_wkt, request.source_path, progress_callback, is_cancelled
        )
        request = replace(request, mask=replace(request.mask, source_path=snapshot_path, layer_name="mask"))
        done = 10.0
    try:
        if request.collect_changes and confirm_registry_addition is not None:
            report_phase("Counting the pixels to change...")
            changed_count = count_recode_changes(
                request,
                progress_callback=_progress_slice(progress_callback, done, done + 15.0),
                is_cancelled=is_cancelled,
            )
            done += 15.0
            if changed_count > WARNING_REGISTRY_LIMIT and not confirm_registry_addition(changed_count):
                raise RasterRecodeCancelled("The global edit was cancelled at the registry warning")
        result = stage_recode(
            request,
            progress_callback=_progress_slice(progress_callback, done, 100.0),
            is_cancelled=is_cancelled,
            phase_callback=report_engine_phase,
        )
    except Exception as error:
        retained = _delete_vector_snapshot(snapshot_path)
        if retained:
            raise RuntimeError(f"{error}; temporary vector-mask files remain at: " + ", ".join(retained)) from error
        raise
    retained = _delete_vector_snapshot(snapshot_path)
    if retained:
        # The edit is staged and verified at this point, and these files are a copy of
        # the mask: they say nothing about the raster.  Failing here would not remove
        # them either, it would only throw the finished work away, so the edit is kept
        # and the leftovers are reported like any other cleanup problem.
        _LOGGER.warning(
            "The global edit continued, but its temporary vector-mask files could not be removed and can be "
            "deleted by hand: %s",
            ", ".join(retained),
        )
    return result


def _dialog_content_widgets(dialog) -> tuple[QWidget, ...]:
    """Return a dialog's own content widgets, excluding any child window.

    Disabling the dialog itself would propagate to the progress dialog parented to
    it and leave its Cancel button dead for the whole edit, so its contents are
    disabled one by one instead.
    """
    return tuple(
        child
        for child in dialog.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if not child.isWindow()
    )


class RasterRecodeTask(QgsTask):
    """Run GDAL staging in a task-manager worker thread."""

    #: Emitted from the worker thread with a description of the step that just started.
    phase_changed = pyqtSignal(str)
    #: Emitted from the worker thread with the number of pixels the edit would add to the
    #: registry.  The worker then waits for :meth:`answer`, or for cancellation.
    confirmation_needed = pyqtSignal(object)

    def __init__(self, request: RecodeRequest, controller, vector_mask_source=None):
        super().__init__("ThRasE global edit", QgsTask.Flag.CanCancel)
        self.request = request
        self.controller = controller
        self.vector_mask_source = vector_mask_source
        self.result: RecodeResult | None = None
        self.error: Exception | None = None
        self.cancelled_by_user = False
        self.warning_messages: tuple[str, ...] = ()
        self._answer_given = threading.Event()
        self._confirmed = False

    def answer(self, confirmed: bool) -> None:
        """Deliver the user's answer to :attr:`confirmation_needed`; called on the main thread."""
        self._confirmed = confirmed
        self._answer_given.set()

    def _confirm_registry_addition(self, changed_count: int) -> bool:
        """Ask the main thread and wait for its answer; cancellation ends the wait as a refusal."""
        self._answer_given.clear()
        self.confirmation_needed.emit(changed_count)
        while not self._answer_given.wait(0.05):
            if self.isCanceled():
                return False
        return self._confirmed

    def run(self):
        with _collect_worker_messages() as collector:
            try:
                self.result = stage_recode_with_qgis_mask(
                    self.request,
                    self.vector_mask_source,
                    progress_callback=self.setProgress,
                    is_cancelled=self.isCanceled,
                    report_phase=self.phase_changed.emit,
                    confirm_registry_addition=self._confirm_registry_addition,
                )
                return True
            except RasterRecodeCancelled:
                self.cancelled_by_user = True
                return False
            except Exception as error:
                self.error = error
                return False
            finally:
                self.warning_messages = tuple(collector.messages)

    def finished(self, result):
        if self.controller is not None:
            self.controller.task_finished(self, result)


class RasterRecodeController(QObject):
    """Own one task and finalize its QGIS-facing work on the main thread."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.task: RasterRecodeTask | None = None
        self.progress: QProgressDialog | None = None
        self.layer_to_edit = None
        self.source_dialog: QDialog | None = None
        self.disabled_widgets: tuple[QWidget, ...] = ()
        self.on_success: CompletionCallback | None = None
        self.on_no_changes: CompletionCallback | None = None
        self.registry_pixels = ()
        self.committing = False

    @property
    def active(self):
        return self.task is not None

    def start(
        self,
        request: RecodeRequest,
        layer_to_edit,
        *,
        parent,
        source_dialog: QDialog | None = None,
        on_success: CompletionCallback | None = None,
        on_no_changes: CompletionCallback | None = None,
        vector_mask_layer=None,
    ):
        """Run ``request`` on a background task and finish it on the main thread."""
        if self.active:
            self._message_bar(source_dialog).pushMessage(
                "Another global edit is already running",
                level=Qgis.MessageLevel.Warning,
                duration=10,
            )
            return False

        progress = None
        try:
            registry_pixels = tuple(layer_to_edit.pixel_log_store)
            request = replace(
                request,
                registry_points=tuple((pixel.x(), pixel.y()) for pixel in registry_pixels),
            )
            vector_mask_source = (
                QgsVectorLayerFeatureSource(vector_mask_layer) if vector_mask_layer is not None else None
            )
            task = RasterRecodeTask(request, self, vector_mask_source)
            task.setDependentLayers(self._dependent_layers(request, layer_to_edit, vector_mask_layer))
            progress = QProgressDialog("Starting the global edit...", "Cancel", 0, 100, parent)
            progress.setWindowTitle("ThRasE global editing")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setAutoClose(False)
            progress.setAutoReset(False)
            progress.setMinimumDuration(0)
            progress.setValue(0)
        except Exception as error:
            if progress is not None:
                progress.close()
                progress.deleteLater()
            self._message_bar(source_dialog).pushMessage(
                f"ERROR: Unable to prepare the global edit: {error}",
                level=Qgis.MessageLevel.Critical,
                duration=20,
            )
            return False

        self.layer_to_edit = layer_to_edit
        self.source_dialog = source_dialog
        self.on_success = on_success
        self.on_no_changes = on_no_changes
        self.registry_pixels = registry_pixels
        self.committing = False
        self.task = task
        self.progress = progress
        try:
            self.owner.set_global_edit_active(True)
            self._disable_source_dialog()
            self.task.progressChanged.connect(lambda value: self.progress and self.progress.setValue(round(value)))
            self.task.phase_changed.connect(self._show_phase)
            self.task.confirmation_needed.connect(self._confirm_registry_addition)
            self.progress.canceled.connect(self.cancel)
            self.progress.show()
            task_manager = QgsApplication.taskManager()
            scheduled = task_manager is not None and task_manager.addTask(self.task)
        except Exception as error:
            self._show_error(f"Unable to schedule the global edit: {error}")
            self._finish_ui()
            return False
        if not scheduled:
            self._show_error("Unable to schedule the global edit")
            self._finish_ui()
            return False
        return True

    @staticmethod
    def _dependent_layers(request: RecodeRequest, layer_to_edit, vector_mask_layer):
        """List every project layer the task must not have edited underneath it."""
        dependent_layers = [layer_to_edit.qgs_layer]
        if vector_mask_layer is not None:
            dependent_layers.append(vector_mask_layer)
        if not isinstance(request.mask, RasterMaskSpec):
            return dependent_layers
        project = QgsProject.instance()
        for layer in project.mapLayers().values() if project is not None else ():
            layer_source = get_source_from(layer)
            if not layer_source:
                continue
            try:
                matches_mask = os.path.samefile(layer_source, request.mask.source_path)
            except OSError:
                matches_mask = os.path.realpath(layer_source) == os.path.realpath(request.mask.source_path)
            if matches_mask and all(layer.id() != dependent.id() for dependent in dependent_layers):
                dependent_layers.append(layer)
        return dependent_layers

    def cancel(self):
        if self.task is not None and not self.committing:
            self.task.cancel()
            if self.progress is not None:
                self.progress.setLabelText("Cancelling after the current raster operation...")

    def _show_phase(self, label):
        """Name the step that is running; a pending cancellation keeps its own label."""
        if self.progress is None or self.committing:
            return
        if self.task is not None and self.task.isCanceled():
            return
        self.progress.setLabelText(label)

    def _confirm_registry_addition(self, changed_count):
        """Ask before a large registry addition while the worker waits for the answer.

        The count comes from a read-only pass over the source, so cancelling here
        costs nothing: the raster has not been copied yet.  The registry has no size
        limit and the threshold is an internal guard, so the message does not quote it.
        """
        task = self.task
        if task is None:
            return
        reply = QMessageBox.warning(
            self.source_dialog or self.owner,
            "ThRasE registry",
            f"This global edit will change {changed_count:,} pixels, and all of them will be added to the "
            "registry. A large registry could use more memory, take longer to display, and make the "
            "configuration file larger.\n\n"
            "Continue and add them to the registry?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        task.answer(reply == QMessageBox.StandardButton.Ok)

    def task_finished(self, task: RasterRecodeTask, successful: bool):
        if task is not self.task:
            if task.result is not None:
                self._discard_result(task.result)
            return

        for warning_message in task.warning_messages:
            self._message_bar().pushMessage(warning_message, level=Qgis.MessageLevel.Warning, duration=20)

        result = task.result
        if task.cancelled_by_user or task.isCanceled():
            if result is not None:
                self._discard_result(result)
            self._message_bar().pushMessage(
                "The global edit was cancelled; the source raster was not changed",
                level=Qgis.MessageLevel.Info,
                duration=10,
            )
            self._finish_ui()
            return
        if not successful or task.error is not None or result is None:
            if result is not None:
                self._discard_result(result)
            self._show_error(str(task.error or "The global edit failed"))
            self._finish_ui()
            return
        if result.status is RecodeStatus.NO_CHANGES:
            try:
                if self.on_no_changes is not None:
                    self._run_callback(self.on_no_changes, result)
                else:
                    self._message_bar().pushMessage(
                        "No changes were applied: no pixels matched the recode criteria",
                        level=Qgis.MessageLevel.Info,
                        duration=10,
                    )
            finally:
                self._finish_ui()
            return

        # A large registry addition was already confirmed by the worker, before the raster was copied.
        record_changes = task.request.collect_changes
        if self.layer_to_edit is None or self.layer_to_edit is not self._current_layer_to_edit():
            self._discard_result(result)
            self._show_error("The active thematic layer changed before the global edit could be committed")
            self._finish_ui()
            return
        current_registry_pixels = self.layer_to_edit.pixel_log_store
        if len(current_registry_pixels) != len(self.registry_pixels) or any(
            pixel not in current_registry_pixels for pixel in self.registry_pixels
        ):
            self._discard_result(result)
            self._show_error("The pixel registry changed before the global edit could be committed")
            self._finish_ui()
            return

        self.committing = True
        if self.progress is not None:
            self.progress.setLabelText("Replacing the raster and updating the registry...")
            self.progress.setCancelButton(None)
            self.progress.setValue(100)

        try:
            outcome = commit_and_reconcile(
                result,
                self.layer_to_edit,
                record_changes=record_changes,
                registry_pixels=self.registry_pixels,
                registry_widget=getattr(self.owner, "registry_widget", None),
            )
        except Exception as error:
            # The raster was not replaced, so the staged files are only kept when
            # the engine says they need manual recovery.
            self._discard_result(result, keep_for_recovery=isinstance(error, RasterRecodeRecoveryError))
            self._show_error(str(error))
            self._finish_ui()
            return

        self._report_outcome(outcome)
        if outcome.recovery_error is not None:
            self._finish_ui()
            return

        completed_result = replace(
            result, status=RecodeStatus.COMMITTED, stage_path=None, lock_path=None, lock_token=None
        )
        self.owner.editing_status.setText(f"{result.changed_count} pixels edited!")
        try:
            if self.on_success is not None:
                self._run_callback(self.on_success, completed_result)
        finally:
            self._finish_ui()

    def _report_outcome(self, outcome):
        """Report everything that happened after the raster was already replaced.

        These messages go to the main window: on success the mask dialog closes
        immediately, so its own message bar would take them out of sight.
        """
        if outcome.recovery_error is not None:
            self.owner.MsgBar.pushMessage(
                f"ERROR: {outcome.recovery_error}", level=Qgis.MessageLevel.Critical, duration=20
            )
            return
        if outcome.cleanup_error is not None:
            self.owner.MsgBar.pushMessage(
                f"The global edit succeeded, but its temporary files could not be removed: {outcome.cleanup_error}. "
                "Remaining files: " + ", ".join(outcome.leftovers),
                level=Qgis.MessageLevel.Warning,
                duration=20,
            )
        if outcome.retained_backups:
            self.owner.MsgBar.pushMessage(
                "The original raster was kept because another program wrote to it after the global edit. Check it "
                "and remove it manually to allow further global edits: " + ", ".join(outcome.retained_backups),
                level=Qgis.MessageLevel.Warning,
                duration=20,
            )
        if outcome.registry_error is not None:
            self.owner.MsgBar.pushMessage(
                f"The raster was edited, but the registry could not be updated: {outcome.registry_error}",
                level=Qgis.MessageLevel.Warning,
                duration=20,
            )

    @staticmethod
    def _current_layer_to_edit():
        from ThRasE.core.editing import LayerToEdit

        return LayerToEdit.current

    def _message_bar(self, source_dialog=None):
        """Return the message bar the user can actually see.

        A modal mask dialog covers the main window, so while one is open its own
        message bar is the only visible one.
        """
        dialog = source_dialog if source_dialog is not None else self.source_dialog
        bar = getattr(dialog, "MsgBar", None) if dialog is not None else None
        return bar if bar is not None else self.owner.MsgBar

    def _show_error(self, message):
        self._message_bar().pushMessage(f"ERROR: {message}", level=Qgis.MessageLevel.Critical, duration=20)

    def _disable_source_dialog(self):
        """Keep the user from changing the request while the edit runs."""
        self.disabled_widgets = ()
        if self.source_dialog is None:
            return
        disabled = tuple(widget for widget in _dialog_content_widgets(self.source_dialog) if widget.isEnabled())
        for widget in disabled:
            widget.setEnabled(False)
        self.disabled_widgets = disabled

    def _restore_source_dialog(self):
        """Re-enable exactly the widgets that were disabled, leaving the rest alone."""
        for widget in self.disabled_widgets:
            try:
                widget.setEnabled(True)
            except RuntimeError:
                pass  # the dialog was destroyed while the edit was running
        self.disabled_widgets = ()

    def _discard_result(self, result, *, keep_for_recovery=False):
        retained = discard_staged(result, keep_for_recovery=keep_for_recovery)
        if retained:
            self._message_bar().pushMessage(
                "Global edit files remain beside the raster and block further global edits until they are checked "
                "and removed: " + ", ".join(retained),
                level=Qgis.MessageLevel.Warning,
                duration=20,
            )
        return retained

    def _run_callback(self, callback, result):
        try:
            callback(result)
        except Exception as error:
            self.owner.MsgBar.pushMessage(
                f"The raster operation succeeded, but its completion action failed: {error}",
                level=Qgis.MessageLevel.Warning,
                duration=20,
            )

    def shutdown(self, timeout_ms=30_000):
        """Cancel and synchronously drain a worker before plugin resources are removed.

        Replacing the raster cannot be interrupted, but it is a short sequence of
        renames, so it is waited out instead of refusing to shut down.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while self.committing and time.monotonic() < deadline:
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 25)
        if self.committing:
            return False
        task = self.task
        if task is None:
            return True
        self.owner.close_after_global_edit_cancel = False
        task.cancel()
        while True:
            remaining_ms = round((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return False
            if task.waitForFinished(min(remaining_ms, 50)):
                break
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 25)
        if self.task is task:
            task.controller = None
            if task.result is not None:
                self._discard_result(task.result)
            self._finish_ui(schedule_close=False)
        return True

    def _finish_ui(self, *, schedule_close=True):
        if self.progress is not None:
            self.progress.close()
            self.progress.deleteLater()
        self._restore_source_dialog()
        self.progress = None
        self.source_dialog = None
        self.layer_to_edit = None
        self.on_success = None
        self.on_no_changes = None
        self.registry_pixels = ()
        self.task = None
        self.committing = False
        self.owner.set_global_edit_active(False)
        if schedule_close and getattr(self.owner, "close_after_global_edit_cancel", False):
            self.owner.close_after_global_edit_cancel = False
            QTimer.singleShot(0, self.owner.close)
