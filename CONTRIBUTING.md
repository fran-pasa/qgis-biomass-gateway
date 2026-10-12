# Contributing to BIOMASS Gateway

---

## 1. Development setup

The repository uses [**uv**](https://docs.astral.sh/uv/) for high-speed, reproducible Python environment and
dependency management.

### Prerequisites
* **QGIS 3.44 LTR or newer** installed on your machine.
* [**uv**](https://docs.astral.sh/uv/) installed:
  ```bash
  # Linux & macOS
  curl -LsSf https://astral.sh/uv/install.sh | sh

  # Windows (PowerShell)
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

### Clone repo
```
git clone https://github.com/your-username/qgis-biomass-gateway.git
cd qgis-biomass-gateway
```

### Install dev dependencies (ruff, pytest, qgis-stubs)
```
uv sync
```
## 2. Linking the plugin to QGIS
Create a symlink from your repository's biomass_gateway/ package
directly into your active QGIS user profile.
### Linux
```
ln -s "$(pwd)/biomass_gateway" "$HOME/.local/share/QGIS/QGIS3/profiles/default/python/plugins/biomass_gateway"
```
### macOS
```
ln -s "$(pwd)/biomass_gateway" "$HOME/Library/Application
Support/QGIS/QGIS3/profiles/default/python/plugins/biomass_gateway"
```
### Windows
On Windows, run your terminal (PowerShell or Command Prompt) as Administrator, or enable Windows Developer Mode to 
allow symlink creation.

PowerShell:
```
New-Item -ItemType SymbolicLink -Path "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\biomass_gateway" 
-Target "$((Get-Location).Path)\biomass_gateway"
```
Command Prompt (cmd.exe):
```
mklink /D "%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\biomass_gateway" "%cd%\biomass_gateway"
```

### Load in QGIS
After following the instructions above, you will be able to find the plugin in the QGIS 
Plugin manager, in the `Installed` tab; just check the box to activate it.

## 3. Codebase Architecture

This is work in progress but the idea is to keep the codebase modularized, in order to allwo multiple
contributors to work concurrently reducing potential git merge conflicts:

```
qgis-biomass-gateway/
├── pyproject.toml              
├── README.md                   
├── CONTRIBUTING.md             
├── biomass_gateway/            
│   ├── metadata.txt            # QGIS plugin manifest 
│   ├── plugin.py               # Plugin lifecycle, menu actions, toolbars
│   ├── icon.png                # Plugin UI icon
│   ├── core/                   # domain models & utilities
│   │   ├── models.py           # BiomassProduct, WarpConfig, GcpPoint
│   │   ├── exceptions.py       # custom domain exceptions
│   │   └── logger.py           # QgsMessageLog & logging bridge
│   ├── reader/                 # Product inspection & canvas loading
│   │   ├── folder_reader.py    # XML annotation & GCP parser
│   │   └── loader.py           # Layer group builder & SAR false-color symbology
│   ├── georeferencing/         # GDAL warping engine
│   │   └── warper.py           # Thin Plate Spline warping
│   ├── processing/             # QGIS Processing provider
│   │   ├── provider.py         # BiomassProcessingProvider registry
│   │   └── algorithms/         # Isolated contributor Processing algorithms
│   ├── maap/                   # STAC catalog client & token authentication
│   └── ui/                     # PyQGIS dock widget & tab panels
│       └── dock_widget.py      # Main dock widget layout
└── tests/                      # unit tests
```

## 4. Development instructions

### Proper dependencies management
To ensure the plugin installs correctly for any QGIS user:
* keep `dependencies = []` in `pyproject.toml`
* Rely on standard libs, QGIS APIs (`qgis.core`, `qgis.gui`) and `osgeo.gdal`
* Dev tools (pytest, ruff, etc.) are restricted to `dependency-groups.dev`


### Code style & quality

Before opening a pull request, ensure your code passes formatting and linting:

Check code style
```
uv run ruff check .
```

Automatically fix linting issues
```
uv run ruff check --fix .
```

Format code
```
uv run ruff format .
```

Run test suite
```
uv run pytest tests/
```

### Git branching

• Clone the repository
• Create a dedicated feature branch, e.g.:
```
git checkout -b feat/pauli-decomposition
```

• Write concise commit messages following standard conventions (feat: ..., fix: ..., docs: ...).


