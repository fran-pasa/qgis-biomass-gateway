"""
Unified logging for Biomass Gateway in QGIS and standalone environments
"""
import logging

LOGGER_NAME = "BiomassGateway"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a scoped logger under the BiomassGateway namespace
    """
    subname = f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME
    return logging.getLogger(subname)


def log_qgis_info(message: str, tag: str = "Biomass Gateway") -> None:
    """Log an informational message to QgsMessageLog if running inside QGIS
    """
    try:
        from qgis.core import Qgis, QgsMessageLog

        QgsMessageLog.logMessage(message, tag, Qgis.MessageLevel.Info)
    except ImportError:
        pass


def log_qgis_warning(message: str, tag: str = "Biomass Gateway") -> None:
    """Log a warning message to QgsMessageLog if running inside QGIS
    """
    try:
        from qgis.core import Qgis, QgsMessageLog

        QgsMessageLog.logMessage(message, tag, Qgis.MessageLevel.Warning)
    except ImportError:
        pass


def log_qgis_critical(message: str, tag: str = "Biomass Gateway") -> None:
    """Log a critical error message to QgsMessageLog if running inside QGIS
    """
    try:
        from qgis.core import Qgis, QgsMessageLog

        QgsMessageLog.logMessage(message, tag, Qgis.MessageLevel.Critical)
    except ImportError:
        pass
