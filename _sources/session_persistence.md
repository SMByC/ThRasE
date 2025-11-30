# Session Persistence

```{image} images/save.webp
:width: 40%
:align: center
```
<br>

```{image} images/load.webp
:width: 35%
:align: center
```
<br>
ThRasE supports session persistence through save and restore functionality that captures your complete workspace state. This capability is essential for multi-session workflows where you need to maintain consistency across editing or review periods.
<br>

- **Multi-session workflow support** - maintain consistency across editing or review periods
- **Team collaboration** - share standardized configurations to ensure uniform inspection methodology
- **Resume capability** - resume work exactly where you left off, preserving both spatial progress and workspace setup

```{tip}
Inside the configuration YAML file, ThRasE saves relative paths for all layers configured with respect to the YAML file’s location, but only when those layers are in the same directory or a subdirectory of the YAML file. This ensures the project remains portable and easy to share.
```

```{important}
**Web/network layers** - if using web or network layers (Google, Esri, Google Earth Engine, XYZ), first save and load your QGIS project, then load the ThRasE configuration file (`.yaml`)
```
