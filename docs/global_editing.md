# Global Editing Tools

```{image} images/global_editing_tools.webp
:width: 60%
:align: center
```
<br>
ThRasE provides two powerful global editing options that allow you to apply changes to multiple areas simultaneously.

## Apply to Whole Image

This option applies the changes defined in the pixel recoding table to the entire thematic raster.

```{warning}
This operation cannot be undone, so use with caution.
```

## Apply from Thematic Classes

```{image} images/global_editing_classes.webp
:width: 60%
:align: center
```
<br>

ThRasE enables you to apply recode pixel table changes selectively within areas defined by classes from another categorical raster file. This capability is crucial when corrections need to respect existing spatial boundaries or land management units. For example, you might need to reclassify forest types only within protected areas, correct agricultural classes exclusively in irrigated zones, or refine land cover classifications within specific administrative boundaries. This targeted approach improves efficiency by avoiding unnecessary edits across the entire map and reduces the risk of inadvertently modifying correctly classified regions, ultimately supporting more precise and contextually appropriate post-classification corrections.

```{warning}
The categorical raster file used to define constraint areas must have the same projection, pixel size, and extent as your thematic map
```
