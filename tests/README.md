# Test organization

Tests are permanent behavioral checks, organized by subsystem. Regressions belong
beside the behavior they protect, rather than in files named after a review or
implementation session.

| Module | Behavior protected |
| --- | --- |
| `test_editing.py` | Manual raster writes, numeric/`NBITS` validation, the integer-band gate, pixel identity and boundaries, provider state, recode-table consistency, lazy raster-clipped selection scans |
| `test_view_widget.py` | Per-view and per-target undo/redo, band switching, delayed picker edits, target removal, callback lifetimes, canvas refresh/CRS, shared opacity, drawing cleanup |
| `test_navigation.py` | Tile generation and traversal, single-tile bounds, survey-foot units, target-specific controls, preview reuse and AOI cleanup |
| `test_registry.py` | Group membership, atomic rebuilds, recorded/unrecorded edits, browser state and lossless export |
| `test_session.py` | Atomic saving, serialization/replacement failures, Save As and Save-and-close outcomes, copied configurations, relative paths (including legacy bare filenames) and provider/sublayer identity, and sessions whose navigation can no longer be rebuilt |
| `test_yaml_restore.py` | Legacy YAML compatibility, symbology restoration/merge choices, configuration-load cancellation and KML escaping |
| `test_plugin_lifecycle.py` | Plugin unload and cleanup of hidden dialogs across editing sessions |
| `test_raster_recode.py` | GDAL global-edit staging, masks, numeric ranges, metadata, cancellation, rollback and recovery |
| `test_raster_recode_task.py` | QGIS task/controller behavior, provider release/reload, progress, cancellation, unload coordination and deferred close |
| `test_autofill.py` | Recode expressions and autofill behavior |

`conftest.py` provides shared QGIS fixtures. `editable_raster` creates an isolated
two-band raster. `editing_ui` creates a real dialog and editing view without
modal startup: it wires the view's signals, while tests invoke main-dialog slots
explicitly. This distinction matters when adding signal-wiring tests.

## Running

The standard commands and local QGIS Python path are documented in `AGENTS.md`.
On the local Python 3.14/QGIS installation:

```sh
PYTHONPATH=/usr/lib/python3.14/site-packages uv run --python 3.14 pytest tests/
```

Coverage is supplementary evidence, not a guarantee that every UI interaction is
tested. Automated tests do not replace manual checks across the supported QGIS/Qt
versions and operating systems. Large manual selections still use synchronous
writes and in-memory undo; their clipping/laziness tests do not establish bounded
total memory or task cancellation support.
