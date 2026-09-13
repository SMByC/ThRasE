"""Bounded-memory, transactional raster recoding.

This is the GDAL-only engine behind ThRasE's global edits.  It never imports
QGIS, so it can run inside a ``QgsTask`` worker thread and be tested without a
QGIS application.

An edit is split into small steps so the GUI can release QGIS providers on the
main thread between preparing the edit and replacing the file:

1. :func:`stage_recode` copies the raster into a private staging directory
   beside the source, recodes the selected band window by window, verifies the
   written windows by reading them back, and clears derived data (statistics,
   histograms, overviews) that the edit invalidated.  The source is only read.
2. :func:`commit_staged` renames the original files into a private backup
   directory and renames the staged files into place.  Both are same-directory
   renames, so no bytes are copied and the original always exists on disk.
3. :func:`rollback_commit` reverses those renames when QGIS cannot reload the
   edited raster.
4. :func:`finalize_commit` deletes the backup once the edited raster is in use,
   unless a program wrote to the backup after the commit.
5. :func:`discard_staged` removes the staging directory of an abandoned edit.

:func:`count_recode_changes` reads the source once to report how many pixels an
edit would change, so a caller can ask about a large registry addition before
any of the above starts.

A lock file beside the raster keeps two ThRasE sessions from editing the same
file at once, and every transaction directory is named after the raster so
:func:`find_transaction_leftovers` can report it after a crash.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
from osgeo import gdal, gdal_array, ogr, osr

DEFAULT_MEMORY_BUDGET_BYTES = 64 * 1024 * 1024
SUPPORTED_RECODE_DRIVERS = frozenset({"GTiff", "HFA"})
#: Cleanup problems that must not abort an edit are logged here rather than raised,
#: so a worker thread can collect the records it produced and report them itself.
GLOBAL_EDIT_LOGGER_NAME = "ThRasE.global_edit"

_LOGGER = logging.getLogger(GLOBAL_EDIT_LOGGER_NAME)
_LOGGER.setLevel(logging.WARNING)

_LOSSY_COMPRESSIONS = frozenset({"JPEG", "WEBP", "JXL"})
_DERIVED_METADATA_PREFIXES = ("STATISTICS_", "HISTOGRAM_")
_STAGE_PURPOSE = "stage"
_BACKUP_PURPOSE = "backup"
_TRANSACTION_PURPOSES = (_STAGE_PURPOSE, _BACKUP_PURPOSE)
_GDAL_CACHE_LOCK = threading.RLock()

ProgressCallback = Callable[[float], None]
CancellationCallback = Callable[[], bool]


class RasterRecodeError(RuntimeError):
    """Raised when a raster cannot be recoded safely; nothing was left behind."""


class RasterRecodeCancelled(RasterRecodeError):
    """Raised when the caller cancels before the source is replaced."""


class RasterRecodeRecoveryError(RasterRecodeError):
    """Raised when files were left in place for manual recovery.

    The message names the directories involved.  The raster stays locked until
    they are inspected and removed.
    """


class RecodeStatus(Enum):
    STAGED = "staged"
    COMMITTED = "committed"
    NO_CHANGES = "no_changes"


class RecodePhase(Enum):
    """Steps of :func:`stage_recode`, reported so a GUI can say what is running.

    Progress alone cannot: copying the raster reports no progress, and the same
    0-100 range covers both the recode and its verification.
    """

    COPYING = "copying"
    RECODING = "recoding"
    VERIFYING = "verifying"


PhaseCallback = Callable[[RecodePhase], None]


@dataclass(frozen=True)
class RasterMaskSpec:
    """Restrict the edit to pixels whose co-located mask pixel has a selected class."""

    source_path: str
    band: int
    selected_values: tuple[int, ...]


@dataclass(frozen=True)
class VectorMaskSpec:
    """Restrict the edit to pixels whose centre falls inside polygon features.

    The features must already be on disk.  GUI callers snapshot the live QGIS
    layer to a temporary GeoPackage first, so uncommitted edits are included
    without holding every geometry in memory.
    """

    source_path: str | None = None
    layer_name: str | None = None
    crs_wkt: str = ""


MaskSpec = RasterMaskSpec | VectorMaskSpec


@dataclass(frozen=True)
class RecodeRequest:
    source_path: str
    band: int
    recode_pairs: tuple[tuple[int, int], ...]
    mask: MaskSpec | None = None
    collect_changes: bool = False
    memory_budget_bytes: int = DEFAULT_MEMORY_BUDGET_BYTES
    window_width: int | None = None
    window_height: int | None = None
    registry_points: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class FileStamp:
    """Cheap change detector for one file: its size, modification time, and inode.

    ``name`` identifies the file: a name inside a known directory for the raster
    being edited, or an absolute path for files that are only read.
    """

    name: str
    size: int
    modified_ns: int
    inode: int


@dataclass(frozen=True)
class ChangeRecord:
    row: int
    column: int
    old_value: int
    new_value: int


@dataclass(frozen=True, eq=False)
class ChangeSet:
    """Every pixel a staged edit changed, kept as arrays so a large edit stays compact.

    The registry has no size limit, so one edit may change millions of pixels.
    Rows and columns are 32-bit (GDAL raster dimensions always fit) and values keep
    the band's own type, so a changed pixel costs about ten bytes; one Python
    object per pixel would cost more than ten times that.  ``len()`` is the number
    of changed pixels, and iterating yields :class:`ChangeRecord` objects in
    chunks so callers can stay simple.
    """

    rows: np.ndarray
    columns: np.ndarray
    old_values: np.ndarray
    new_values: np.ndarray

    @classmethod
    def empty(cls) -> ChangeSet:
        nothing = np.empty(0, dtype=np.int32)
        return cls(nothing, nothing, nothing, nothing)

    @classmethod
    def concatenate(cls, parts: Sequence[ChangeSet]) -> ChangeSet:
        if not parts:
            return cls.empty()
        return cls(
            np.concatenate([part.rows for part in parts]),
            np.concatenate([part.columns for part in parts]),
            np.concatenate([part.old_values for part in parts]),
            np.concatenate([part.new_values for part in parts]),
        )

    def __len__(self) -> int:
        return int(self.rows.size)

    def __iter__(self) -> Iterator[ChangeRecord]:
        chunk = 65_536
        for start in range(0, len(self), chunk):
            stop = start + chunk
            yield from map(
                ChangeRecord,
                self.rows[start:stop].tolist(),
                self.columns[start:stop].tolist(),
                self.old_values[start:stop].tolist(),
                self.new_values[start:stop].tolist(),
            )


@dataclass(frozen=True)
class RecodeResult:
    """Outcome of :func:`stage_recode`.

    ``source_files`` and ``stage_files`` hold file names, not paths: every file of
    a dataset lives beside its main file, and the staged copy keeps the same names
    inside the staging directory.  ``changes`` holds every changed pixel when the
    request asked for them, and ``None`` otherwise.
    """

    status: RecodeStatus
    source_path: str
    stage_path: str | None
    changed_count: int
    changes: ChangeSet | None
    registry_values: tuple[int | None, ...]
    geotransform: tuple[float, ...]
    source_files: tuple[str, ...]
    stage_files: tuple[str, ...]
    source_stamps: tuple[FileStamp, ...]
    stage_stamps: tuple[FileStamp, ...]
    lock_path: str | None
    lock_token: str | None


@dataclass(frozen=True)
class CommitReceipt:
    """Outcome of :func:`commit_staged`, consumed by rollback and finalization."""

    source_path: str
    stage_path: str
    backup_path: str
    backup_files: tuple[str, ...]
    installed_files: tuple[str, ...]
    backup_stamps: tuple[FileStamp, ...]
    lock_path: str
    lock_token: str


# --------------------------------------------------------------------------------------
# Small shared helpers


def _never_cancel() -> bool:
    return False


def _ignore_progress(_progress: float) -> None:
    return None


def _ignore_phase(_phase: RecodePhase) -> None:
    return None


def _check_cancelled(is_cancelled: CancellationCallback) -> None:
    if is_cancelled():
        raise RasterRecodeCancelled("Raster recoding was cancelled")


def _with_bounded_gdal_cache(operation):
    """Cap GDAL's shared block cache to a quarter of the request budget while staging."""

    @functools.wraps(operation)
    def bounded(request: RecodeRequest, *args, **kwargs):
        with _GDAL_CACHE_LOCK:
            previous_limit = gdal.GetCacheMax()
            transaction_limit = max(1, request.memory_budget_bytes // 4)
            limited = transaction_limit < previous_limit
            if limited:
                gdal.SetCacheMax(transaction_limit)
            try:
                return operation(request, *args, **kwargs)
            finally:
                if limited:
                    gdal.SetCacheMax(previous_limit)

    return bounded


def _check_gdal_status(status, message: str) -> None:
    if status not in (None, gdal.CE_None):
        raise RasterRecodeError(message)


def _close_dataset_checked(dataset, message: str) -> None:
    """Flush and close a writable dataset, surfacing errors GDAL only reports at close."""
    gdal.ErrorReset()
    _check_gdal_status(dataset.FlushCache(), message)
    close = getattr(dataset, "Close", None)
    if close is not None:
        _check_gdal_status(close(), message)
    elif gdal.GetLastErrorType() >= gdal.CE_Failure:
        raise RasterRecodeError(message)


def _existing_local_file(source_path: str, description: str) -> Path:
    if not source_path or source_path.startswith("/vsi") or "://" in source_path:
        raise RasterRecodeError(f"Global editing requires a local {description} file")
    try:
        path = Path(source_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RasterRecodeError(f'{description.capitalize()} file does not exist: "{source_path}"') from error
    if not path.is_file():
        raise RasterRecodeError(f'{description.capitalize()} is not a regular file: "{source_path}"')
    return path


def _canonical_local_path(source_path: str) -> str:
    """Resolve the raster to edit and require a writable directory for the transaction files."""
    path = _existing_local_file(source_path, "raster")
    if not os.access(path.parent, os.W_OK):
        raise RasterRecodeError(f'Raster directory is not writable: "{path.parent}"')
    return str(path)


def _dataset_file_names(dataset, dataset_path: str) -> tuple[str, ...]:
    """Return the names of every file GDAL associates with ``dataset_path``.

    Sidecars (``.msk``, ``.aux.xml``, ``.ige``, ``.rrd``...) are renamed together
    with the main file, so they must be regular files in the same directory.
    """
    main = Path(dataset_path).resolve()
    names = set()
    for file_name in dataset.GetFileList() or [dataset_path]:
        if not file_name or file_name.startswith("/vsi") or "://" in file_name:
            raise RasterRecodeError("All files associated with the raster must be local")
        path = Path(file_name)
        if not path.is_absolute():
            path = main.parent / path
        try:
            path = path.resolve(strict=True)
        except OSError as error:
            raise RasterRecodeError(f'Raster sidecar does not exist: "{path}"') from error
        if path.parent != main.parent or not path.is_file():
            raise RasterRecodeError(f'Raster sidecars must be regular files beside the raster: "{path}"')
        names.add(path.name)
    if main.name not in names:
        raise RasterRecodeError("The raster dataset file list does not contain its main file")
    return tuple(sorted(names))


def _referenced_local_files(dataset, dataset_path: str) -> tuple[str, ...]:
    """Return the absolute path of every local file a read-only dataset is built from.

    Nothing here is ever renamed, so unlike the raster being edited these files may
    live in any directory.  A ``.vrt`` mask referencing rasters elsewhere is therefore
    accepted, and each referenced file is stamped so an external change is detected.
    """
    parent = Path(dataset_path).resolve().parent
    paths = set()
    for file_name in dataset.GetFileList() or [dataset_path]:
        if not file_name or file_name.startswith("/vsi") or "://" in file_name:
            raise RasterRecodeError("All files of the raster mask must be local")
        path = Path(file_name)
        if not path.is_absolute():
            path = parent / path
        try:
            path = path.resolve(strict=True)
        except OSError as error:
            raise RasterRecodeError(f'A file of the raster mask does not exist: "{path}"') from error
        if path.is_file():
            paths.add(str(path))
    return tuple(sorted(paths))


def _stamp_one(path: Path, name: str) -> FileStamp:
    stat = os.stat(path)
    return FileStamp(name, stat.st_size, stat.st_mtime_ns, stat.st_ino)


def _stamp_files(directory: Path, names: Iterable[str]) -> tuple[FileStamp, ...]:
    """Stamp files of the raster being edited, which all live in one known directory."""
    return tuple(_stamp_one(directory / name, name) for name in names)


def _stamp_paths(paths: Iterable[str]) -> tuple[FileStamp, ...]:
    """Stamp files that are only read, which may live anywhere on the local filesystem."""
    return tuple(_stamp_one(Path(path), path) for path in paths)


def _files_changed(directory: Path, names: Sequence[str], stamps: Sequence[FileStamp]) -> bool:
    """Report whether any file differs from its recorded stamp (a missing file counts as changed)."""
    try:
        return _stamp_files(directory, names) != tuple(stamps)
    except OSError:
        return True


def _paths_changed(paths: Sequence[str], stamps: Sequence[FileStamp]) -> bool:
    try:
        return _stamp_paths(paths) != tuple(stamps)
    except OSError:
        return True


def _main_last(names: Iterable[str], main_name: str) -> tuple[str, ...]:
    """Order file names so the main raster file is handled after its sidecars."""
    return tuple(sorted(names, key=lambda name: name == main_name))


def _main_first(names: Iterable[str], main_name: str) -> tuple[str, ...]:
    return tuple(reversed(_main_last(names, main_name)))


# --------------------------------------------------------------------------------------
# Lock file and transaction directories


def _lock_path_for(source_path: str) -> str:
    path = Path(source_path)
    return str(path.with_name(f".{path.name}.thrase.lock"))


def _acquire_lock(source_path: str) -> tuple[str, str]:
    """Create the per-raster lock file exclusively and return its path and owner token."""
    lock_path = _lock_path_for(source_path)
    token = uuid.uuid4().hex
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RasterRecodeRecoveryError(
            f'Another ThRasE edit owns this raster. Remove "{lock_path}" only if no edit is running.'
        ) from error
    except OSError as error:
        raise RasterRecodeError(f'Unable to create the raster lock "{lock_path}": {error}') from error
    details = {"version": 2, "token": token, "pid": os.getpid(), "source_path": source_path, "created": time.time()}
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(details, stream, sort_keys=True)
    except OSError as error:
        _remove_file_quietly(lock_path)
        raise RasterRecodeError(f'Unable to write the raster lock "{lock_path}": {error}') from error
    return lock_path, token


def _validate_lock(lock_path: str, token: str) -> None:
    try:
        with open(lock_path, encoding="utf-8") as stream:
            owner = json.load(stream).get("token")
    except (OSError, ValueError, AttributeError) as error:
        raise RasterRecodeRecoveryError(f'The raster lock "{lock_path}" is missing or unreadable') from error
    if owner != token:
        raise RasterRecodeRecoveryError(f'The raster lock "{lock_path}" belongs to another edit')


def _release_lock(lock_path: str, token: str) -> None:
    """Remove the lock if it still exists and this transaction owns it."""
    if not os.path.exists(lock_path):
        return
    _validate_lock(lock_path, token)
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise RasterRecodeError(f'Unable to remove the raster lock "{lock_path}": {error}') from error


def _release_lock_quietly(lock_path: str, token: str) -> None:
    try:
        _release_lock(lock_path, token)
    except RasterRecodeError as error:
        _LOGGER.warning("%s", error)


def _remove_file_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _create_transaction_directory(source_path: str, purpose: str) -> Path:
    """Create a private, uniquely named directory beside the raster for one transaction step."""
    source = Path(source_path)
    directory = source.with_name(f".{source.name}.thrase-{purpose}-{uuid.uuid4().hex}")
    try:
        directory.mkdir(mode=0o700)
    except OSError as error:
        raise RasterRecodeError(f'Unable to create the transaction directory "{directory}": {error}') from error
    return directory


def _remove_transaction_directory(directory: Path) -> None:
    """Delete a transaction directory ThRasE created, with every file inside it."""
    if not directory.exists():
        return
    try:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
    except OSError as error:
        raise RasterRecodeError(f'Unable to remove the transaction directory "{directory}": {error}') from error


def _remove_transaction_directory_quietly(directory: Path) -> None:
    try:
        _remove_transaction_directory(directory)
    except RasterRecodeError as error:
        _LOGGER.warning("%s", error)


def _sync_directory(directory: Path) -> None:
    """Flush directory metadata so completed renames survive a power loss (best effort)."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return  # Windows cannot open directories this way; NTFS journals renames itself.
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def find_transaction_leftovers(source_path: str) -> tuple[str, ...]:
    """Return the lock file and staging or backup directories left beside a raster.

    A finished edit leaves nothing behind, so anything found here belongs to an
    edit still running in another session or to one that was interrupted.
    """
    source = Path(source_path).expanduser().resolve(strict=False)
    leftovers = []
    lock_path = _lock_path_for(str(source))
    if os.path.exists(lock_path):
        leftovers.append(lock_path)
    for purpose in _TRANSACTION_PURPOSES:
        for directory in source.parent.glob(f".{source.name}.thrase-{purpose}-*"):
            if directory.is_dir():
                leftovers.append(str(directory))
    return tuple(sorted(leftovers))


# --------------------------------------------------------------------------------------
# Request validation


def _integer_dtype(data_type: int) -> np.dtype:
    supported = {gdal.GDT_Byte, gdal.GDT_UInt16, gdal.GDT_Int16, gdal.GDT_UInt32, gdal.GDT_Int32}
    for name in ("GDT_Int8", "GDT_UInt64", "GDT_Int64"):
        value = getattr(gdal, name, None)
        if value is not None:
            supported.add(value)
    if data_type not in supported:
        raise RasterRecodeError(
            f"The selected band must use a scalar integer data type, not {gdal.GetDataTypeName(data_type)}"
        )
    numpy_type = gdal_array.GDALTypeCodeToNumericTypeCode(data_type)
    if numpy_type is None:
        raise RasterRecodeError(f"GDAL cannot map {gdal.GetDataTypeName(data_type)} to a NumPy data type")
    return np.dtype(numpy_type)


def _effective_integer_limits(dataset, band, dtype: np.dtype) -> tuple[int, int, str]:
    """Return the value range the band can store, honouring packed ``NBITS`` profiles."""
    if not hasattr(gdal, "GDT_Int8"):
        pixel_type = (
            band.GetMetadataItem("PIXELTYPE", "IMAGE_STRUCTURE")
            or dataset.GetMetadataItem("PIXELTYPE", "IMAGE_STRUCTURE")
            or ""
        ).upper()
        if pixel_type == "SIGNEDBYTE" and band.DataType == gdal.GDT_Byte:
            raise RasterRecodeError(
                "Legacy PIXELTYPE=SIGNEDBYTE rasters are not supported for global editing; convert the raster to Int8"
            )

    limits = np.iinfo(dtype)
    nbits_value = band.GetMetadataItem("NBITS", "IMAGE_STRUCTURE") or dataset.GetMetadataItem(
        "NBITS", "IMAGE_STRUCTURE"
    )
    if not nbits_value:
        return int(limits.min), int(limits.max), dtype.name
    try:
        nbits = int(nbits_value)
    except (TypeError, ValueError) as error:
        raise RasterRecodeError(f"The raster has an invalid NBITS value: {nbits_value}") from error
    storage_bits = dtype.itemsize * 8
    if nbits < 1 or nbits > storage_bits:
        raise RasterRecodeError(f"The raster has an invalid NBITS={nbits} profile for {dtype.name}")
    if np.issubdtype(dtype, np.signedinteger):
        minimum = -(1 << (nbits - 1))
        maximum = (1 << (nbits - 1)) - 1
    else:
        minimum = 0
        maximum = (1 << nbits) - 1
    return minimum, maximum, f"NBITS={nbits} {dtype.name}"


def _validated_pairs(request: RecodeRequest, limits: tuple[int, int, str], nodata) -> tuple[tuple[int, int], ...]:
    if not request.recode_pairs:
        raise RasterRecodeError("There are no recode values to apply")
    if request.memory_budget_bytes < 1:
        raise RasterRecodeError("The raster memory budget must be greater than zero")

    minimum, maximum, range_name = limits
    pairs = []
    seen = set()
    for old_value, new_value in request.recode_pairs:
        old_value = int(old_value)
        new_value = int(new_value)
        if old_value in seen:
            raise RasterRecodeError(f"The recode table contains duplicate source value {old_value}")
        seen.add(old_value)
        if not minimum <= old_value <= maximum:
            raise RasterRecodeError(f"Source value {old_value} is outside the {range_name} range")
        if not minimum <= new_value <= maximum:
            raise RasterRecodeError(f"New value {new_value} is outside the {range_name} range")
        if nodata is not None and old_value == nodata:
            raise RasterRecodeError(f"The active NoData value {old_value} cannot be recoded")
        if old_value != new_value:
            pairs.append((old_value, new_value))
    if not pairs:
        raise RasterRecodeError("There are no non-identity recode values to apply")
    return tuple(pairs)


def _open_source(source_path: str, request: RecodeRequest):
    dataset = gdal.Open(source_path, gdal.GA_ReadOnly)
    if dataset is None:
        raise RasterRecodeError(f'Unable to open raster "{source_path}"')
    driver_name = dataset.GetDriver().ShortName
    if driver_name not in SUPPORTED_RECODE_DRIVERS:
        supported = ", ".join(sorted(SUPPORTED_RECODE_DRIVERS))
        raise RasterRecodeError(
            f"Global editing does not support the {driver_name} driver. Convert the raster to {supported}."
        )
    if not 1 <= request.band <= dataset.RasterCount:
        raise RasterRecodeError(f"Raster band {request.band} does not exist")
    layout = (dataset.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") or "").upper()
    if layout == "COG":
        raise RasterRecodeError("Cloud Optimized GeoTIFFs must be converted to a standard GeoTIFF before editing")
    compression = _compression_name(dataset)
    if compression in _LOSSY_COMPRESSIONS:
        raise RasterRecodeError(f"Lossy {compression} compression is not supported for categorical editing")
    return dataset


def _compression_name(dataset) -> str:
    return (dataset.GetMetadataItem("COMPRESSION", "IMAGE_STRUCTURE") or "NONE").upper()


def _check_free_space(
    dataset, dtype: np.dtype, directory: Path, names: Sequence[str], memory_budget_bytes: int
) -> None:
    """Require room for the staging copy, plus growth of the edited band when it is compressed."""
    source_size = sum(os.path.getsize(directory / name) for name in names)
    growth = 0 if _compression_name(dataset) == "NONE" else dataset.RasterXSize * dataset.RasterYSize * dtype.itemsize
    if shutil.disk_usage(directory).free < source_size + growth + memory_budget_bytes:
        raise RasterRecodeError("There is not enough free disk space to create a staging copy of the raster")


# --------------------------------------------------------------------------------------
# Geometry helpers


def _same_crs(first_wkt: str, second_wkt: str) -> bool:
    if not first_wkt and not second_wkt:
        return True
    if not first_wkt or not second_wkt:
        return False
    first = osr.SpatialReference()
    second = osr.SpatialReference()
    if first.ImportFromWkt(first_wkt) != 0 or second.ImportFromWkt(second_wkt) != 0:
        return False
    if hasattr(osr, "OAMS_TRADITIONAL_GIS_ORDER"):
        first.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        second.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    first.AutoIdentifyEPSG()
    second.AutoIdentifyEPSG()
    first_authority = (first.GetAuthorityName(None), first.GetAuthorityCode(None))
    second_authority = (second.GetAuthorityName(None), second.GetAuthorityCode(None))
    if first_authority[0] and first_authority == second_authority:
        return True
    return bool(first.IsSame(second, ["IGNORE_DATA_AXIS_TO_SRS_AXIS_MAPPING=YES"]))


def _close_enough(first: float, second: float, tolerance: float = 1e-9) -> bool:
    return abs(first - second) <= tolerance * max(1.0, abs(first), abs(second))


def _window_shape(request: RecodeRequest, width: int, height: int, bytes_per_cell: int) -> tuple[int, int]:
    """Choose a window so the working arrays stay within half the memory budget."""
    max_cells = max(1, request.memory_budget_bytes // max(1, 2 * bytes_per_cell))
    window_width = request.window_width or min(width, max_cells)
    window_height = request.window_height or min(height, max(1, max_cells // window_width))
    if window_width < 1 or window_height < 1:
        raise RasterRecodeError("Raster window dimensions must be greater than zero")
    window_width = min(width, window_width)
    window_height = min(height, window_height)
    if window_width * window_height > max_cells:
        raise RasterRecodeError("Configured raster windows exceed the memory budget")
    return window_width, window_height


def _windows(width: int, height: int, window_width: int, window_height: int):
    for yoff in range(0, height, window_height):
        ysize = min(window_height, height - yoff)
        for xoff in range(0, width, window_width):
            yield xoff, yoff, min(window_width, width - xoff), ysize


def _window_count(width: int, height: int, window_width: int, window_height: int) -> int:
    return math.ceil(width / window_width) * math.ceil(height / window_height)


def _registry_probe_windows(
    points: Iterable[tuple[float, float]],
    geotransform,
    raster_width: int,
    raster_height: int,
    window_width: int,
    window_height: int,
):
    """Group registry pixels by the window that contains them; points outside the raster stay ``None``."""
    inverse = gdal.InvGeoTransform(geotransform)
    if inverse is None:
        raise RasterRecodeError("The target raster geotransform is not invertible")
    grouped: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    values: list[int | None] = []
    for index, (x_coord, y_coord) in enumerate(points):
        column_value, row_value = gdal.ApplyGeoTransform(inverse, x_coord, y_coord)
        column = math.floor(column_value)
        row = math.floor(row_value)
        values.append(None)
        if not (0 <= column < raster_width and 0 <= row < raster_height):
            continue
        grouped.setdefault((column // window_width, row // window_height), []).append((index, row, column))
    return grouped, values


def _window_polygon(geotransform, xoff: int, yoff: int, xsize: int, ysize: int):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for column, row in (
        (xoff, yoff),
        (xoff + xsize, yoff),
        (xoff + xsize, yoff + ysize),
        (xoff, yoff + ysize),
        (xoff, yoff),
    ):
        x_coord, y_coord = gdal.ApplyGeoTransform(geotransform, column, row)
        ring.AddPoint_2D(x_coord, y_coord)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


# --------------------------------------------------------------------------------------
# Mask readers


class _RasterMaskReader:
    """Select target pixels whose co-located pixel in another categorical raster has a chosen class.

    The mask must share the target's pixel grid; its extent may differ by whole
    pixels.  NoData and pixels flagged invalid by the mask's GDAL validity band
    are never selected.  The mask is only read, so any local GDAL raster works,
    including a ``.vrt`` built from rasters in other directories.
    """

    def __init__(self, spec: RasterMaskSpec, target_geotransform, target_projection: str):
        self.source_path = str(_existing_local_file(spec.source_path, "raster mask"))
        self.dataset: Any = gdal.Open(self.source_path, gdal.GA_ReadOnly)
        if self.dataset is None:
            raise RasterRecodeError(f'Unable to open raster mask "{spec.source_path}"')
        self.band: Any = None
        self.validity_mask: Any = None
        try:
            if not 1 <= spec.band <= self.dataset.RasterCount:
                raise RasterRecodeError(f"Raster mask band {spec.band} does not exist")
            if not spec.selected_values:
                raise RasterRecodeError("At least one raster-mask class must be selected")
            if not _same_crs(target_projection, self.dataset.GetProjection()):
                raise RasterRecodeError("The raster mask and target raster use different coordinate systems")
            mask_geotransform = self.dataset.GetGeoTransform()
            for target_value, mask_value in zip(
                (target_geotransform[1], target_geotransform[2], target_geotransform[4], target_geotransform[5]),
                (mask_geotransform[1], mask_geotransform[2], mask_geotransform[4], mask_geotransform[5]),
                strict=True,
            ):
                if not _close_enough(target_value, mask_value):
                    raise RasterRecodeError("The raster mask must use the same pixel grid as the target raster")
            inverse = gdal.InvGeoTransform(target_geotransform)
            if inverse is None:
                raise RasterRecodeError("The target raster geotransform is not invertible")
            offset_x, offset_y = gdal.ApplyGeoTransform(inverse, mask_geotransform[0], mask_geotransform[3])
            rounded_x = round(offset_x)
            rounded_y = round(offset_y)
            if not _close_enough(offset_x, rounded_x, 1e-7) or not _close_enough(offset_y, rounded_y, 1e-7):
                raise RasterRecodeError("The raster mask origin is not aligned to the target pixel grid")
            self.column_offset = int(rounded_x)
            self.row_offset = int(rounded_y)
            self.selected_values = spec.selected_values
            self.band = self.dataset.GetRasterBand(spec.band)
            self.nodata = self.band.GetNoDataValue()
            self.validity_mask = self.band.GetMaskBand()
            self.mask_is_all_valid = bool(self.band.GetMaskFlags() & gdal.GMF_ALL_VALID)
            self.file_paths = _referenced_local_files(self.dataset, self.source_path)
            self.stamps = _stamp_paths(self.file_paths)
        except Exception:
            self.close()
            raise

    @property
    def itemsize(self) -> int:
        numpy_type = gdal_array.GDALTypeCodeToNumericTypeCode(self.band.DataType)
        if numpy_type is None:
            raise RasterRecodeError("GDAL cannot map the raster-mask data type to NumPy")
        return np.dtype(numpy_type).itemsize

    def selection(self, xoff: int, yoff: int, xsize: int, ysize: int) -> np.ndarray:
        selected = np.zeros((ysize, xsize), dtype=np.bool_)
        target_x0 = max(xoff, self.column_offset)
        target_y0 = max(yoff, self.row_offset)
        target_x1 = min(xoff + xsize, self.column_offset + self.dataset.RasterXSize)
        target_y1 = min(yoff + ysize, self.row_offset + self.dataset.RasterYSize)
        if target_x0 >= target_x1 or target_y0 >= target_y1:
            return selected

        mask_xoff = target_x0 - self.column_offset
        mask_yoff = target_y0 - self.row_offset
        width = target_x1 - target_x0
        height = target_y1 - target_y0
        values = self.band.ReadAsArray(mask_xoff, mask_yoff, width, height)
        if values is None:
            raise RasterRecodeError("Unable to read a raster-mask window")
        window_selected = np.zeros(values.shape, dtype=np.bool_)
        class_match = np.empty(values.shape, dtype=np.bool_)
        for selected_value in self.selected_values:
            np.equal(values, selected_value, out=class_match)
            np.logical_or(window_selected, class_match, out=window_selected)
        if self.nodata is not None:
            np.not_equal(values, self.nodata, out=class_match)
            np.logical_and(window_selected, class_match, out=window_selected)
        if not self.mask_is_all_valid:
            valid = self.validity_mask.ReadAsArray(mask_xoff, mask_yoff, width, height)
            if valid is None:
                raise RasterRecodeError("Unable to read a raster-mask validity window")
            np.not_equal(valid, 0, out=class_match)
            np.logical_and(window_selected, class_match, out=window_selected)
        dest_x = target_x0 - xoff
        dest_y = target_y0 - yoff
        selected[dest_y : dest_y + height, dest_x : dest_x + width] = window_selected
        return selected

    def validate_unchanged(self) -> None:
        if _paths_changed(self.file_paths, self.stamps):
            raise RasterRecodeError("The raster mask changed while the global edit was running")

    def close(self) -> None:
        self.validity_mask = None
        self.band = None
        self.dataset = None


class _VectorMaskReader:
    """Select target pixels whose centre lies inside polygon features (``ALL_TOUCHED=FALSE``)."""

    def __init__(self, spec: VectorMaskSpec, target_geotransform, target_projection: str):
        self.target_geotransform = target_geotransform
        self.target_projection = target_projection
        self.owned_dataset: Any = None
        self.layer: Any = None

        if not spec.source_path:
            raise RasterRecodeError("The vector mask has no readable features")
        self.owned_dataset = ogr.Open(spec.source_path, 0)
        if self.owned_dataset is None:
            raise RasterRecodeError(f'Unable to open vector mask "{spec.source_path}"')
        self.layer = (
            self.owned_dataset.GetLayerByName(spec.layer_name) if spec.layer_name else self.owned_dataset.GetLayer(0)
        )
        if self.layer is None:
            raise RasterRecodeError("Unable to open the requested vector-mask layer")

        layer_srs = self.layer.GetSpatialRef()
        layer_wkt = layer_srs.ExportToWkt() if layer_srs is not None else spec.crs_wkt
        if not _same_crs(target_projection, layer_wkt):
            raise RasterRecodeError("The vector mask and target raster use different coordinate systems")

    @property
    def itemsize(self) -> int:
        return 1

    def selection(self, xoff: int, yoff: int, xsize: int, ysize: int) -> np.ndarray:
        window_gt = (
            self.target_geotransform[0] + xoff * self.target_geotransform[1] + yoff * self.target_geotransform[2],
            self.target_geotransform[1],
            self.target_geotransform[2],
            self.target_geotransform[3] + xoff * self.target_geotransform[4] + yoff * self.target_geotransform[5],
            self.target_geotransform[4],
            self.target_geotransform[5],
        )
        mask_dataset = gdal.GetDriverByName("MEM").Create("", xsize, ysize, 1, gdal.GDT_Byte)
        if mask_dataset is None:
            raise RasterRecodeError("Unable to allocate a vector-mask window")
        mask_dataset.SetGeoTransform(window_gt)
        mask_dataset.SetProjection(self.target_projection)
        mask_band = mask_dataset.GetRasterBand(1)
        mask_band.Fill(0)

        self.layer.SetSpatialFilter(_window_polygon(self.target_geotransform, xoff, yoff, xsize, ysize))
        try:
            status = gdal.RasterizeLayer(mask_dataset, [1], self.layer, burn_values=[1], options=["ALL_TOUCHED=FALSE"])
        finally:
            self.layer.SetSpatialFilter(None)
        if status != gdal.CE_None:
            raise RasterRecodeError("Unable to rasterize a vector-mask window")
        selected = mask_band.ReadAsArray(0, 0, xsize, ysize)
        mask_band = None
        mask_dataset = None
        if selected is None:
            raise RasterRecodeError("Unable to read a vector-mask window")
        return selected.astype(np.bool_, copy=False)

    def validate_unchanged(self) -> None:
        return None

    def close(self) -> None:
        self.layer = None
        self.owned_dataset = None


def _open_mask(mask: MaskSpec | None, geotransform, projection: str):
    if isinstance(mask, RasterMaskSpec):
        return _RasterMaskReader(mask, geotransform, projection)
    if isinstance(mask, VectorMaskSpec):
        return _VectorMaskReader(mask, geotransform, projection)
    return None


# --------------------------------------------------------------------------------------
# Recoding passes


def _apply_recode(original: np.ndarray, pairs: Sequence[tuple[int, int]], selected: np.ndarray | None):
    """Return the recoded window and a boolean mask of the pixels that changed.

    Every mapping is matched against the unchanged input, so ``1 -> 2`` and
    ``2 -> 3`` applied together never cascade a ``1`` into a ``3``.
    """
    recoded = original.copy()
    match = np.empty(original.shape, dtype=np.bool_)
    for old_value, new_value in pairs:
        np.equal(original, old_value, out=match)
        if selected is not None:
            np.logical_and(match, selected, out=match)
        recoded[match] = new_value
    np.not_equal(original, recoded, out=match)
    return recoded, match


def _read_window(band, xoff: int, yoff: int, xsize: int, ysize: int, description: str) -> np.ndarray:
    values = band.ReadAsArray(xoff, yoff, xsize, ysize)
    if values is None:
        raise RasterRecodeError(f"Unable to read the {description} at column {xoff}, row {yoff}")
    return values


@dataclass(frozen=True)
class _Windowing:
    width: int
    height: int
    window_width: int
    window_height: int

    def __iter__(self):
        return _windows(self.width, self.height, self.window_width, self.window_height)

    def __len__(self) -> int:
        return _window_count(self.width, self.height, self.window_width, self.window_height)


def _recode_pass(source_band, stage_band, pairs, mask_reader, windowing: _Windowing, progress, is_cancelled) -> int:
    """Write every window that the recode changes and return the number of changed pixels."""
    changed_count = 0
    for index, (xoff, yoff, xsize, ysize) in enumerate(windowing, start=1):
        _check_cancelled(is_cancelled)
        original = _read_window(source_band, xoff, yoff, xsize, ysize, "source raster")
        selected = mask_reader.selection(xoff, yoff, xsize, ysize) if mask_reader is not None else None
        recoded, changed = _apply_recode(original, pairs, selected)
        window_changed = int(np.count_nonzero(changed))
        if window_changed:
            _check_gdal_status(
                stage_band.WriteArray(recoded, xoff, yoff),
                f"Unable to write the staging raster at column {xoff}, row {yoff}",
            )
            changed_count += window_changed
        progress(index * 55.0 / len(windowing))
    return changed_count


def _verify_pass(
    source_band,
    stage_band,
    pairs,
    mask_reader,
    windowing: _Windowing,
    request: RecodeRequest,
    geotransform,
    progress,
    is_cancelled,
):
    """Re-read the staged band from disk and check it against the recode recomputed from the source.

    The same pass counts the changed pixels, collects every changed pixel for the
    registry when the request asks for them, and reads back the current value of
    every registry pixel.
    """
    changed_count = 0
    collected: list[ChangeSet] | None = [] if request.collect_changes else None
    probes, registry_values = _registry_probe_windows(
        request.registry_points,
        geotransform,
        windowing.width,
        windowing.height,
        windowing.window_width,
        windowing.window_height,
    )
    for index, (xoff, yoff, xsize, ysize) in enumerate(windowing, start=1):
        _check_cancelled(is_cancelled)
        original = _read_window(source_band, xoff, yoff, xsize, ysize, "source raster")
        actual = _read_window(stage_band, xoff, yoff, xsize, ysize, "staging raster")
        selected = mask_reader.selection(xoff, yoff, xsize, ysize) if mask_reader is not None else None
        expected, changed = _apply_recode(original, pairs, selected)
        if not np.array_equal(actual, expected):
            raise RasterRecodeError(f"The staged raster does not hold the recoded values at column {xoff}, row {yoff}")
        window_changed = int(np.count_nonzero(changed))
        changed_count += window_changed
        if collected is not None and window_changed:
            rows, columns = np.nonzero(changed)
            collected.append(
                ChangeSet(
                    (rows + yoff).astype(np.int32),
                    (columns + xoff).astype(np.int32),
                    original[rows, columns],
                    actual[rows, columns],
                )
            )
        for registry_index, row, column in probes.get(
            (xoff // windowing.window_width, yoff // windowing.window_height), ()
        ):
            registry_values[registry_index] = int(actual[row - yoff, column - xoff])
        progress(55.0 + index * 40.0 / len(windowing))
    changes = ChangeSet.concatenate(collected) if collected is not None else None
    return changed_count, changes, tuple(registry_values)


def _clear_derived_data(dataset, band) -> None:
    """Remove data the edit invalidated while keeping the class metadata it did not.

    Statistics, histograms, and overviews are computed from pixel values, so they
    are stale after a recode.  Color tables, category names, and attribute-table
    rows are keyed by pixel value and stay valid; only their per-class counts are
    stale, so statistics columns are dropped from the attribute table.
    """
    for key in tuple(band.GetMetadata() or {}):
        if key.upper().startswith(_DERIVED_METADATA_PREFIXES):
            _check_gdal_status(band.SetMetadataItem(key, None), f"Unable to remove stale raster metadata {key}")
    attribute_table = band.GetDefaultRAT()
    if attribute_table is not None and hasattr(attribute_table, "RemoveStatistics"):
        cleaned = attribute_table.Clone()
        cleaned.RemoveStatistics()
        if cleaned.GetColumnCount() != attribute_table.GetColumnCount():
            _check_gdal_status(band.SetDefaultRAT(cleaned), "Unable to update the raster attribute table")
    if any(dataset.GetRasterBand(index).GetOverviewCount() for index in range(1, dataset.RasterCount + 1)):
        _check_gdal_status(dataset.BuildOverviews("NONE", []), "Unable to remove stale raster overviews")


def _copy_dataset_files(driver, destination: str, source: str):
    return driver.CopyFiles(destination, source)


def _copy_permissions(
    source_directory: Path,
    source_names: Sequence[str],
    stage_directory: Path,
    stage_names: Sequence[str],
    main_name: str,
) -> None:
    """Give every staged file the permission bits of its source counterpart.

    Ownership is copied only when the process is allowed to (usually root); a
    file created by the current user otherwise keeps the current owner.
    """
    for name in stage_names:
        reference = source_directory / (name if name in source_names else main_name)
        target = stage_directory / name
        try:
            shutil.copymode(reference, target)
        except OSError as error:
            raise RasterRecodeError(f'Unable to preserve the permissions of "{target}": {error}') from error
        if hasattr(os, "chown"):
            reference_stat = os.stat(reference)
            target_stat = os.stat(target)
            if (reference_stat.st_uid, reference_stat.st_gid) != (target_stat.st_uid, target_stat.st_gid):
                try:
                    os.chown(target, reference_stat.st_uid, reference_stat.st_gid)
                except OSError:
                    pass


# --------------------------------------------------------------------------------------
# Public transaction steps


@_with_bounded_gdal_cache
def count_recode_changes(
    request: RecodeRequest,
    *,
    progress_callback: ProgressCallback = _ignore_progress,
    is_cancelled: CancellationCallback = _never_cancel,
) -> int:
    """Count the pixels ``request`` would change, reading the source once and writing nothing.

    This is the cheap part of an edit: :func:`stage_recode` copies the raster,
    then reads and writes it several more times.  Counting first lets a caller
    warn about a large registry addition before any of that work starts, and
    cancelling at that point leaves nothing behind.  The request is validated the
    same way staging validates it, so an edit that cannot run fails here.
    """
    source_path = str(_existing_local_file(request.source_path, "raster"))
    source_dataset = None
    source_band = None
    mask_reader = None
    try:
        source_dataset = _open_source(source_path, request)
        source_band = source_dataset.GetRasterBand(request.band)
        dtype = _integer_dtype(source_band.DataType)
        limits = _effective_integer_limits(source_dataset, source_band, dtype)
        pairs = _validated_pairs(request, limits, source_band.GetNoDataValue())
        geotransform = tuple(source_dataset.GetGeoTransform())
        mask_reader = _open_mask(request.mask, geotransform, source_dataset.GetProjection())

        # Working arrays per cell: original, recoded copy, changed mask, mask values.
        bytes_per_cell = 2 * dtype.itemsize + 1 + (mask_reader.itemsize + 1 if mask_reader is not None else 0)
        window_width, window_height = _window_shape(
            request, source_dataset.RasterXSize, source_dataset.RasterYSize, bytes_per_cell
        )
        windowing = _Windowing(source_dataset.RasterXSize, source_dataset.RasterYSize, window_width, window_height)
        changed_count = 0
        for index, (xoff, yoff, xsize, ysize) in enumerate(windowing, start=1):
            _check_cancelled(is_cancelled)
            original = _read_window(source_band, xoff, yoff, xsize, ysize, "source raster")
            selected = mask_reader.selection(xoff, yoff, xsize, ysize) if mask_reader is not None else None
            _recoded, changed = _apply_recode(original, pairs, selected)
            changed_count += int(np.count_nonzero(changed))
            progress_callback(index * 100.0 / len(windowing))
        return changed_count
    finally:
        if mask_reader is not None:
            mask_reader.close()
        source_band = None
        source_dataset = None


@_with_bounded_gdal_cache
def stage_recode(
    request: RecodeRequest,
    *,
    progress_callback: ProgressCallback = _ignore_progress,
    is_cancelled: CancellationCallback = _never_cancel,
    phase_callback: PhaseCallback = _ignore_phase,
) -> RecodeResult:
    """Recode ``request.band`` into a staged copy beside the source raster.

    The source is only read.  The result is ``STAGED`` when the staged copy awaits
    :func:`commit_staged`, or ``NO_CHANGES`` when nothing matched and nothing was
    left on disk.  Any failure or cancellation removes the staging directory and
    releases the lock before the exception propagates.  ``phase_callback``
    receives each :class:`RecodePhase` as it starts.
    """
    source_path = _canonical_local_path(request.source_path)
    leftovers = find_transaction_leftovers(source_path)
    if leftovers:
        raise RasterRecodeRecoveryError(
            "Files from an unfinished ThRasE edit must be checked and removed before this raster can be edited "
            "again: " + ", ".join(leftovers)
        )
    lock_path, lock_token = _acquire_lock(source_path)
    stage_directory: Path | None = None
    try:
        stage_directory = _create_transaction_directory(source_path, _STAGE_PURPOSE)
        result = _stage_in_directory(
            request,
            source_path,
            stage_directory,
            lock_path,
            lock_token,
            progress_callback,
            is_cancelled,
            phase_callback,
        )
        if result.status is RecodeStatus.NO_CHANGES:
            _remove_transaction_directory(stage_directory)
            _release_lock(lock_path, lock_token)
        return result
    except BaseException:
        # The source was never modified, so there is nothing to recover: leave nothing behind.
        if stage_directory is not None:
            _remove_transaction_directory_quietly(stage_directory)
        _release_lock_quietly(lock_path, lock_token)
        raise


def _stage_in_directory(
    request: RecodeRequest,
    source_path: str,
    stage_directory: Path,
    lock_path: str,
    lock_token: str,
    progress_callback: ProgressCallback,
    is_cancelled: CancellationCallback,
    phase_callback: PhaseCallback,
) -> RecodeResult:
    source_dataset = None
    source_band = None
    stage_dataset = None
    stage_band = None
    mask_reader = None
    try:
        _check_cancelled(is_cancelled)
        source_dataset = _open_source(source_path, request)
        driver = source_dataset.GetDriver()
        source_band = source_dataset.GetRasterBand(request.band)
        dtype = _integer_dtype(source_band.DataType)
        limits = _effective_integer_limits(source_dataset, source_band, dtype)
        pairs = _validated_pairs(request, limits, source_band.GetNoDataValue())
        source = Path(source_path)
        source_names = _dataset_file_names(source_dataset, source_path)
        source_stamps = _stamp_files(source.parent, source_names)
        _check_free_space(source_dataset, dtype, source.parent, source_names, request.memory_budget_bytes)
        geotransform = tuple(source_dataset.GetGeoTransform())
        projection = source_dataset.GetProjection()
        mask_reader = _open_mask(request.mask, geotransform, projection)

        # Working arrays per cell: original, recoded/expected, read-back, two boolean masks, mask values.
        bytes_per_cell = 3 * dtype.itemsize + 2 + (mask_reader.itemsize + 1 if mask_reader is not None else 0)
        window_width, window_height = _window_shape(
            request, source_dataset.RasterXSize, source_dataset.RasterYSize, bytes_per_cell
        )
        windowing = _Windowing(source_dataset.RasterXSize, source_dataset.RasterYSize, window_width, window_height)

        stage_path = str(stage_directory / source.name)
        _check_cancelled(is_cancelled)
        phase_callback(RecodePhase.COPYING)
        if _copy_dataset_files(driver, stage_path, source_path) != gdal.CE_None:
            raise RasterRecodeError("Unable to create a staging copy of the raster")
        _check_cancelled(is_cancelled)
        stage_dataset = gdal.Open(stage_path, gdal.GA_Update)
        if stage_dataset is None:
            raise RasterRecodeError("Unable to open the staging raster for writing")
        stage_band = stage_dataset.GetRasterBand(request.band)

        phase_callback(RecodePhase.RECODING)
        progress_callback(0.0)
        changed_count = _recode_pass(
            source_band, stage_band, pairs, mask_reader, windowing, progress_callback, is_cancelled
        )
        if changed_count:
            _clear_derived_data(stage_dataset, stage_band)
        stage_band = None
        _close_dataset_checked(stage_dataset, "Unable to write the staging raster")
        stage_dataset = None

        if changed_count == 0:
            if mask_reader is not None:
                mask_reader.validate_unchanged()
            progress_callback(100.0)
            return RecodeResult(
                status=RecodeStatus.NO_CHANGES,
                source_path=source_path,
                stage_path=None,
                changed_count=0,
                changes=ChangeSet.empty() if request.collect_changes else None,
                registry_values=(),
                geotransform=geotransform,
                source_files=source_names,
                stage_files=(),
                source_stamps=source_stamps,
                stage_stamps=(),
                lock_path=None,
                lock_token=None,
            )

        stage_dataset = gdal.Open(stage_path, gdal.GA_ReadOnly)
        if stage_dataset is None:
            raise RasterRecodeError("Unable to reopen the staging raster for verification")
        stage_band = stage_dataset.GetRasterBand(request.band)
        phase_callback(RecodePhase.VERIFYING)
        verified_count, changes, registry_values = _verify_pass(
            source_band,
            stage_band,
            pairs,
            mask_reader,
            windowing,
            request,
            geotransform,
            progress_callback,
            is_cancelled,
        )
        if verified_count != changed_count:
            raise RasterRecodeError("The staged raster changed an unexpected number of pixels")
        stage_names = _dataset_file_names(stage_dataset, stage_path)
        stage_band = None
        stage_dataset = None

        _copy_permissions(source.parent, source_names, stage_directory, stage_names, source.name)
        if mask_reader is not None:
            mask_reader.validate_unchanged()
        if _files_changed(source.parent, source_names, source_stamps):
            raise RasterRecodeError("The source raster changed while the global edit was running")
        progress_callback(100.0)
        return RecodeResult(
            status=RecodeStatus.STAGED,
            source_path=source_path,
            stage_path=stage_path,
            changed_count=changed_count,
            changes=changes,
            registry_values=registry_values,
            geotransform=geotransform,
            source_files=source_names,
            stage_files=stage_names,
            source_stamps=source_stamps,
            stage_stamps=_stamp_files(stage_directory, stage_names),
            lock_path=lock_path,
            lock_token=lock_token,
        )
    finally:
        if mask_reader is not None:
            mask_reader.close()
        stage_band = None
        stage_dataset = None
        source_band = None
        source_dataset = None


def _move_files(names: Iterable[str], source_directory: Path, destination_directory: Path, moved: list[str]) -> None:
    """Rename files one by one, recording each success so a later failure can be undone."""
    for name in names:
        os.replace(source_directory / name, destination_directory / name)
        moved.append(name)


def commit_staged(result: RecodeResult) -> CommitReceipt:
    """Replace the source files with the staged files using same-directory renames.

    Every QGIS provider holding the source open must have been released first,
    because Windows refuses to rename open files.  The original files move into a
    private backup directory beside the raster, so no bytes are copied and the
    original always exists on disk.  If any rename fails, the renames already
    done are reversed before the error is raised.
    """
    if (
        result.status is not RecodeStatus.STAGED
        or result.stage_path is None
        or result.lock_path is None
        or result.lock_token is None
    ):
        raise RasterRecodeError("Only a staged raster result can be committed")
    _validate_lock(result.lock_path, result.lock_token)
    source = Path(result.source_path)
    directory = source.parent
    stage_directory = Path(result.stage_path).parent
    if _files_changed(directory, result.source_files, result.source_stamps):
        raise RasterRecodeError("The source raster changed after the edit was prepared, so it was not replaced")
    if _files_changed(stage_directory, result.stage_files, result.stage_stamps):
        raise RasterRecodeError("The staged raster changed after it was verified, so it was not installed")
    for name in result.stage_files:
        if name not in result.source_files and (directory / name).exists():
            raise RasterRecodeError(
                f'Refusing to overwrite "{directory / name}", which appeared beside the raster during the edit'
            )

    backup_directory = _create_transaction_directory(result.source_path, _BACKUP_PURPOSE)
    moved_out: list[str] = []
    installed: list[str] = []
    try:
        _move_files(_main_first(result.source_files, source.name), directory, backup_directory, moved_out)
        _move_files(_main_last(result.stage_files, source.name), stage_directory, directory, installed)
    except OSError as error:
        undo_error = _undo_commit(directory, stage_directory, backup_directory, moved_out, installed)
        if undo_error is not None:
            raise RasterRecodeRecoveryError(
                f"Replacing the raster failed ({error}) and it could not be put back automatically ({undo_error}). "
                f'The original files are in "{backup_directory}" and the edited files in "{stage_directory}".'
            ) from error
        raise RasterRecodeError(f"Unable to replace the raster, so it was left unchanged: {error}") from error
    _sync_directory(directory)
    return CommitReceipt(
        source_path=result.source_path,
        stage_path=result.stage_path,
        backup_path=str(backup_directory / source.name),
        backup_files=tuple(moved_out),
        installed_files=tuple(installed),
        backup_stamps=_stamp_files(backup_directory, moved_out),
        lock_path=result.lock_path,
        lock_token=result.lock_token,
    )


def _undo_commit(
    directory: Path,
    stage_directory: Path,
    backup_directory: Path,
    moved_out: list[str],
    installed: list[str],
) -> OSError | None:
    """Reverse a partially applied commit; return the first error if the reversal itself fails."""
    try:
        _move_files(list(reversed(installed)), directory, stage_directory, [])
        _move_files(list(reversed(moved_out)), backup_directory, directory, [])
        backup_directory.rmdir()
    except OSError as error:
        return error
    return None


def rollback_commit(receipt: CommitReceipt) -> None:
    """Put the original files back after a commit whose QGIS reload failed.

    The edited files return to the staging directory, where :func:`finalize_commit`
    or :func:`discard_staged` deletes them, and the backup files return to the
    source directory.
    """
    _validate_lock(receipt.lock_path, receipt.lock_token)
    source = Path(receipt.source_path)
    directory = source.parent
    stage_directory = Path(receipt.stage_path).parent
    backup_directory = Path(receipt.backup_path).parent
    if _files_changed(backup_directory, receipt.backup_files, receipt.backup_stamps):
        raise RasterRecodeRecoveryError(
            f'The original files in "{backup_directory}" changed after the commit and were not restored automatically'
        )
    try:
        present = [name for name in _main_first(receipt.installed_files, source.name) if (directory / name).exists()]
        _move_files(present, directory, stage_directory, [])
        _move_files(_main_last(receipt.backup_files, source.name), backup_directory, directory, [])
    except OSError as error:
        raise RasterRecodeRecoveryError(
            f'Rollback failed: {error}. The original files remain in "{backup_directory}".'
        ) from error
    _remove_transaction_directory(backup_directory)
    _sync_directory(directory)


def finalize_commit(receipt: CommitReceipt) -> tuple[str, ...]:
    """Remove the backup and staging directories and release the lock.

    The backup is kept, and the path of its main file returned, when any backup
    file changed after the commit: a program that still had the old raster open
    may have written through that handle, and those writes must not be deleted.
    """
    _validate_lock(receipt.lock_path, receipt.lock_token)
    backup_directory = Path(receipt.backup_path).parent
    retained: tuple[str, ...] = ()
    try:
        if backup_directory.exists():
            if _files_changed(backup_directory, receipt.backup_files, receipt.backup_stamps):
                retained = (receipt.backup_path,)
            else:
                _remove_transaction_directory(backup_directory)
        _remove_transaction_directory(Path(receipt.stage_path).parent)
    except BaseException:
        # The cleanup error names the files that stayed behind, so it is the one worth
        # reporting: releasing the lock is still attempted, but only logged if it fails,
        # so it cannot replace that error.
        _release_lock_quietly(receipt.lock_path, receipt.lock_token)
        raise
    _release_lock(receipt.lock_path, receipt.lock_token)
    return retained


def discard_staged(result: RecodeResult, *, keep_for_recovery: bool = False) -> tuple[str, ...]:
    """Remove the staging directory of an abandoned edit and release its lock.

    With ``keep_for_recovery`` nothing is deleted and the lock stays in place, so
    the raster remains blocked until the leftover files have been inspected.
    Returns the transaction paths that still exist afterwards.
    """
    leftovers = []
    if result.stage_path is not None:
        stage_directory = Path(result.stage_path).parent
        if not keep_for_recovery:
            _remove_transaction_directory_quietly(stage_directory)
        if stage_directory.exists():
            leftovers.append(str(stage_directory))
    if result.lock_path is not None and result.lock_token is not None:
        if not keep_for_recovery:
            _release_lock_quietly(result.lock_path, result.lock_token)
        if os.path.exists(result.lock_path):
            leftovers.append(result.lock_path)
    return tuple(leftovers)
