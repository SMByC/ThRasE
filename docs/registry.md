# Modification Tracking System

```{image} images/registry.webp
:width: 90%
:align: center
```
<br>
ThRasE includes a modification tracking system that automatically records all edits to your thematic map during editing sessions.
<br>

- **Complete edit history** - every pixel change is logged with its original value and timestamp, creating a complete edit history with strong traceability for quality assurance
- **Visual modifications** - modified areas can be highlighted directly on the canvas, providing immediate spatial feedback and helping you identify patterns or regions you may have missed during editing
- **Timeline navigation** - use the registry slider to navigate through different stages of your editing process
- **Export capabilities** - save your registry to a vector file with all pixel modifications, including original and new pixel values, and timestamps

```{image} images/export_registry.webp
:width: 90%
:align: center
```
<br>

```{note} 
This comprehensive record supports workflows where transparency and reproducibility are critical, allowing you to verify corrections, trace your decisions, and export change logs for documentation or review purposes.
```

```{tip}
Enable the highlight feature to visualize your editing progress across the map. This helps ensure you haven't accidentally skipped areas or concentrated edits in one region while neglecting others.
```

```{note}
While the registry is enabled (by default), ThRasE will save the registry in the configuration file (YAML) and restore it when loaded. The registry can grow over time for large edits. If it makes ThRasE slow down, consider disabling the registry or using different editing sessions. You can restart the registry by clearing it.
```
