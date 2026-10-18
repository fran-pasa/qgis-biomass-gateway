![biomass.png](biomass.png)
# BIOMASS Gateway - QGIS plugin

This repository contains the code of a QGIS plugin for discovering, inspecting and processing ESA BIOMASS satellite data.
This project was initialized during the ESA Biomass MAAP hackathon 2026 hosted at ESOC.

---

## Overview
    
The **BIOMASS Gateway** plugin is intended to bridge the gap between ESA BIOMASS P-band radar products and
the GIS community. It aims to enable scientists and developers to visualize and analyze BIOMASS
products directly inside QGIS.

---
### Key Capabilities
Still work in progress, but here the functionalities implemented so far are listed
* **Local product inspection & display**: inspect BIOMASS products (L1A–L2B), parse XML annotations and NetCDF LUT metadata, and render amplitude rasters with automatic False-Color RGB (HH-HV-VV) or Grayscale contrast stretching.
* **SAR georeferencing**: geocode slant-range products into map coordinates using NetCDF orbit LUT grids or GCP-based interpolation (TPS and Polynomial orders 1–3).
* **ESA MAAP STAC discovery**: search remote BIOMASS collections by spatial extent (canvas bounds) and date range, preview footprints on the canvas, and authenticate via MAAP Keycloak.
* **QGIS Processing Toolbox**:
  * **Warp product**: georeference rasters or product folders.
  * **Calibrate to Gamma0**: radiometrically calibrate L1 SAR amplitude products to $\gamma^0$ (dB).
  * **Mosaic BIOMASS rasters**: build multi-band VRT mosaics with nodata masking across overlapping swaths and frames.
  * **RFI mitigation**: mitigate radio frequency interference on L1A products via Lambda-1 and Block Subspace Filtering (BSF).

---
### Installation for users
Hopefully soon the plugin will be available in the QGIS Plugin official repository. For the time being, users can follow
the steps below to install it manually.
1. Download the latest `biomass_gateway.zip` from the [**Releases**](https://github.com/fran-pasa/qgis-biomass-gateway/releases) page.
2. In QGIS, navigate to the menu: **Plugins** > **Manage and Install Plugins...**
3. Select the **Install from ZIP** tab on the left sidebar.
4. Browse to the downloaded `.zip` file and click **Install Plugin**.
5. Ensure **Biomass Gateway** is checked in the **Installed** tab.
6. Launch the plugin panel using the **Biomass Gateway** toolbar icon or via **Plugins** > **Biomass Gateway**, or find the algorithms in the **Processing Toolbox** under **BIOMASS Gateway**.

---
### Usage
Work in progress


---
### Contributing
To develop new features, integrate new algorithms or more please check [**`CONTRIBUTING.md`**](CONTRIBUTING.md) for 
local development setup, code architecture, etc.

**Disclaimer**: this is an open-source community tool developed for ESA BIOMASS MAAP hackathon and not an official ESA
product.
