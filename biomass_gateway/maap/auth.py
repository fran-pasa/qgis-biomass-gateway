"""
Authentication module to access data from MAAP STAC.
Handles token management and OAuth2 offline token exchange.
"""

import base64
from dataclasses import dataclass
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from qgis.core import QgsSettings

from ..core.logger import get_logger, log_qgis_info

logger = get_logger("authentication")


def _decode_jwt_claims(jwt_token: str) -> dict:
    """Extract claims from unverified JWT payload without external dependencies
    """
    try:
        parts = jwt_token.split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
            payload_bytes = base64.urlsafe_b64decode(padded.encode("utf-8"))
            return json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        pass
    return {}

# Keycloak OAuth2 Token endpoint for exchanging offline tokens
MAAP_TOKEN_URL = (
    "https://iam.maap.eo.esa.int/realms/esa-maap/protocol/openid-connect/token"
)

# Standard ESA MAAP public client credentials for offline token exchange
MAAP_CLIENT_ID = "offline-token"
MAAP_CLIENT_SECRET = "p1eL7uonXs6MDxtGbgKdPVRAmnGxHpVE"

SETTINGS_TOKEN_KEY = "biomass_gateway/maap_offline_token"


@dataclass(frozen=True)
class AuthValidationResult:
    """Data model to represent the outcome of a token validation process.
    """

    is_valid: bool
    message: str
    username: str | None = None


class MAAPAuthManager:
    """Coordinates authentication tokens and GDAL network options.
    This manager handles MAAP credentials in the plugin, modifying
    QGIS settings and GDAL configs.
    """

    def __init__(self) -> None:
        self.settings = QgsSettings()

        # In-memory cache for the 10-hour short-lived access token
        self._access_token: str | None = None
        self._token_expiry_epoch: float = 0.0

        # if an offline token was saved from a previous session, restore GDAL auth
        if self.get_offline_token():
            try:
                self.get_access_token()
            except Exception as e:
                logger.warning(f"Could not refresh access token on startup: {e}")


    def get_offline_token(self) -> str | None:
        """Retrieve stored MAAP Offline token from QGIS settings
        """
        token = self.settings.value(SETTINGS_TOKEN_KEY, "", type=str).strip()
        return token if token else None


    def save_offline_token(self, token: str) -> None:
        """Persist offline token and immediately fetch a fresh access token for GDAL."""
        clean_token = token.strip()
        self.settings.setValue(SETTINGS_TOKEN_KEY, clean_token)
        self._access_token = None
        self._token_expiry_epoch = 0.0

        # fetch access token and apply to GDAL
        self.get_access_token()
        log_qgis_info("MAAP offline token saved.")
        logger.info("Saved MAAP offline token to QgsSettings.")


    def clear_token(self) -> None:
        """Remove previously saved token and clear GDAL configuration."""
        self.settings.remove(SETTINGS_TOKEN_KEY)
        self._access_token = None
        self._token_expiry_epoch = 0.0
        self.clear_gdal_auth()
        log_qgis_info("MAAP authentication token cleared.")
        logger.info("Cleared MAAP authentication token.")


    def get_access_token(self) -> str | None:
        """Return a valid Bearer access token, refresh if expired
        """
        # return cached token if still valid
        if self._access_token and time.time() < (self._token_expiry_epoch - 60):
            return self._access_token

        # check for configured offline token
        offline_token = self.get_offline_token()
        if not offline_token:
            return None

        # exchange offline token with actual access token
        access_token, expires_in, _ = self._exchange_token(offline_token)
        self._access_token = access_token
        self._token_expiry_epoch = time.time() + expires_in

        # inject access token to gdal
        self.apply_gdal_auth(access_token)
        return self._access_token


    def _exchange_token(self, offline_token: str, timeout: int = 15) -> tuple[str, int, str]:
        """Perform OAuth2 refresh_token grant exchange against Keycloak.

        Returns:
            tuple[str, int, str]: (access_token, expires_in_seconds, username)
        """
        payload = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": MAAP_CLIENT_ID,
            "client_secret": MAAP_CLIENT_SECRET,
            "refresh_token": offline_token.strip(),
        }).encode("utf-8")

        req = urllib.request.Request(
            MAAP_TOKEN_URL,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "QGIS-Biomass-Gateway",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 36000))
            claims = _decode_jwt_claims(access_token)
            username = claims.get("name") or claims.get("preferred_username") or claims.get("email") or "User"
            return access_token, expires_in, username



    def get_headers(self) -> dict[str, str]:
        """Generate HTTP headers dictionary for urllib requests."""
        headers = {
            "User-Agent": "QGIS-Biomass-Gateway",
            "Accept": "application/json",
        }
        token = self.get_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers


    def validate_token(
        self, offline_token: str | None = None, timeout: int = 15
    ) -> AuthValidationResult:
        """Validate offline token
        """
        candidate = (offline_token or self.get_offline_token() or "").strip()
        if not candidate:
            return AuthValidationResult(
                is_valid=False,
                message="Authentication failed: No token provided.",
            )

        try:
            access_token, expires_in, username = self._exchange_token(candidate,
                                                                      timeout=timeout)
            # Store fresh access token and configure GDAL
            self._access_token = access_token
            self._token_expiry_epoch = time.time() + expires_in
            self.apply_gdal_auth(access_token)

            return AuthValidationResult(
                is_valid=True,
                message=f"Authentication successful! Logged in as: {username}",
                username=username,
            )

        except urllib.error.HTTPError as err:
            if err.code in (400, 401):
                return AuthValidationResult(
                    is_valid=False,
                    message="Authentication failed: Invalid or expired offline token.",
                )
            if err.code == 403:
                return AuthValidationResult(
                    is_valid=False,
                    message="Access denied: Account lacks required permissions (403 Forbidden).",
                )
            return AuthValidationResult(
                is_valid=False,
                message=f"HTTP Error {err.code}: {err.reason}",
            )

        except urllib.error.URLError as err:
            return AuthValidationResult(
                is_valid=False,
                message=f"Connection failed: Unable to reach Keycloak ({err.reason})",
            )

        except Exception as err:
            return AuthValidationResult(
                is_valid=False,
                message=f"Validation error: {err}",
            )
