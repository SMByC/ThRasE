# Global Editing Tools

```{image} images/global_editing_tools.webp
:width: 60%
:align: center
```
<br>
ThRasE provides two global editing tools that apply the recode pixel table to many areas at once: to the entire
thematic raster, or only within the areas of a mask.

Global edits run on a background QGIS task and process the raster in bounded windows, so memory use stays within the
configured budget whatever the raster size. When the changes are to be recorded in the registry, ThRasE first counts
the pixels the edit would change, with one read of the raster and before any of the work below starts, and asks for
confirmation if that number is very large.

The original raster is left untouched while ThRasE writes the edited values into a staging copy beside it, reads every
edited window back from disk to verify it, and clears the statistics, histograms, and overviews the edit invalidated.
You can cancel during this phase, although cancellation waits for the active GDAL operation to return. Cancellation is
disabled only for the short final step that releases the matching QGIS providers, swaps the files, and reconnects every
layer using them. Nothing is copied in that final step: the original files are moved into a hidden backup, the edited
copy takes their place, and the move is undone if QGIS cannot reload the result. The backup is deleted once the edited
raster is in use, and file permissions and the layer's NoData and resampling settings are kept.

```{note}
Global editing supports local GeoTIFF (`GTiff`) and Erdas Imagine (`HFA`) rasters whose associated files are stored
beside the main raster. Remote rasters, subdatasets, Cloud Optimized GeoTIFFs, lossy-compressed rasters, legacy
`PIXELTYPE=SIGNEDBYTE` rasters, and other GDAL drivers must first be converted to a standard local GeoTIFF or HFA file.
Packed `NBITS` rasters are accepted only when every old and new class fits their effective bit range.
```

```{warning}
The staging copy needs free space beside the raster roughly equal to the size of the raster's files, plus the
processing-memory budget and room for compression growth of the edited band when the raster is compressed. Existing
overviews/pyramids and cached statistics are removed after a successful edit because they were computed from the old
values; rebuild overviews afterwards if they are needed. Color tables, category names, and raster attribute table
classes are keyed by pixel value and are kept; only their pixel-count and histogram columns are dropped.
```

A hidden lock file beside the raster prevents two ThRasE sessions from editing the same file at once, and the staging
and backup directories are named after the raster (`.<name>.thrase-stage-…` and `.<name>.thrase-backup-…`). A finished
edit leaves none of them behind. If QGIS or the operating system terminates during an edit, or if another program wrote
to the old file after the swap, these files remain: opening ThRasE lists them, and further global edits on that raster
are refused until they have been checked and removed. If the raster itself is missing after a crash, move the files from
the backup directory back beside it.

## Apply to Entire Thematic Raster

This option applies the changes defined in the pixel recoding table to the entire thematic raster.

```{warning}
This operation cannot be undone, so use with caution.
```

## Apply Within Classes or Mask

```{image} images/global_editing_classes.webp
:width: 60%
:align: center
```
<br>

ThRasE enables you to apply recode pixel table changes selectively within areas defined by selected classes of another categorical raster file, or within the polygons of a vector layer. This capability is crucial when corrections need to respect existing spatial boundaries or land management units. For example, you might need to reclassify forest types only within protected areas, correct agricultural classes exclusively in irrigated zones, or refine land cover classifications within specific administrative boundaries. This feature applies more precise and contextually appropriate post-classification corrections to the entire thematic raster.

You can define the mask in two ways:

- **Classes of another raster** — choose a categorical raster and tick the classes whose areas you want to edit. It must
  have the same projection and pixel size as your thematic map and share its pixel grid, but its extent can be smaller,
  larger, or shifted.
- **Polygons of a vector layer** — choose a polygon layer and the changes are applied inside its polygons. A pixel is
  edited only when its centre falls inside a polygon.

```{warning}
The mask must use the same projection as your thematic map, and a raster mask must also match its pixel size and grid.
This operation cannot be undone, so use with caution.
```

## Advanced settings

A global edit works through the raster in pieces rather than loading it whole, so ThRasE can edit a thematic map far
larger than the memory of the computer running it. The processing-memory budget is the most memory one edit may use to
do that, and its default of 64 MiB is enough for a raster of any size. QGIS, Python, and already-loaded plugin data
still have their own memory use on top of it. Advanced users can change the budget from the QGIS Python console; the
value is read when an edit starts:

```python
from qgis.PyQt.QtCore import QSettings

QSettings().setValue("ThRasE/global_edit_memory_mib", 64)
```

**The default suits almost every raster, and having more memory available is not by itself a reason to raise it.** The
pieces an edit works in stay small whatever the budget is, so a larger one was measured to make no meaningful
difference to how long an edit takes, while using proportionally more memory. Two situations are worth changing it for:

- **Memory is tight**, for example on a small machine or while other heavy QGIS work is running: lower the budget. The
  edit still completes, it simply works in smaller pieces.
- **A global edit is unexpectedly slow on a raster stored in large tiles**: raise the budget. ThRasE reads the raster
  in strips, but a tiled raster is stored in square blocks that have to be decompressed whole, so a budget that cannot
  hold a whole row of blocks decompresses the same blocks again for every strip that touches them. Give it at least
  three times one row of blocks:

  ```
  one row of blocks = raster width × block height × bytes per pixel
  ```

  `gdalinfo` reports the block size as `Block=512x512`. A 20000-pixel-wide 32-bit raster with 512-pixel blocks has a
  block row of 39 MiB and therefore wants at least 120 MiB: that edit took 60 seconds at the 64 MiB default and 19
  seconds at 128 MiB. Beyond what the blocks need there is nothing to gain, so raise it to fit them and no further.
