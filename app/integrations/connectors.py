"""External connector boundary (Google Drive, ERP, document repositories, catalogues).

Connectors only run with explicit configuration and authorisation. None of the production connectors
are implemented against live services in this release; they report `available() == False` instead of
returning fake successes. The `local_folder` connector is a working, clearly-synthetic demonstration
that imports files from a configured local directory (used with sample_data/).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.errors import ServiceUnavailable


@dataclass
class RemoteFile:
    name: str
    data: bytes


class Connector(Protocol):
    key: str
    name: str
    description: str

    def available(self) -> bool: ...
    def list_files(self) -> list[str]: ...
    def fetch(self, name: str) -> RemoteFile: ...


class NotConfiguredConnector:
    def __init__(self, key: str, name: str, description: str):
        self.key, self.name, self.description = key, name, description

    def available(self) -> bool:
        return False

    def list_files(self) -> list[str]:
        raise ServiceUnavailable(f"{self.name} is not configured. Add OAuth credentials and authorise access first.")

    def fetch(self, name: str) -> RemoteFile:
        raise ServiceUnavailable(f"{self.name} is not configured.")


class LocalFolderConnector:
    key, name = "local_folder", "Local folder (demo repository)"
    description = "Imports files from TP_CONNECTOR_LOCAL_FOLDER — a simulated company repository for demos."

    def _root(self) -> Path | None:
        p = os.environ.get("TP_CONNECTOR_LOCAL_FOLDER")
        return Path(p).resolve() if p else None

    def available(self) -> bool:
        r = self._root()
        return bool(r and r.is_dir())

    def list_files(self) -> list[str]:
        r = self._root()
        if not r or not r.is_dir():
            raise ServiceUnavailable("TP_CONNECTOR_LOCAL_FOLDER is not set.")
        return sorted(p.name for p in r.iterdir() if p.is_file() and not p.name.startswith("."))

    def fetch(self, name: str) -> RemoteFile:
        r = self._root()
        if not r:
            raise ServiceUnavailable("TP_CONNECTOR_LOCAL_FOLDER is not set.")
        p = (r / name).resolve()
        if p.parent != r or not p.is_file():
            raise ServiceUnavailable("File not found in the connector folder.")
        return RemoteFile(p.name, p.read_bytes())


CONNECTORS: dict[str, Connector] = {
    "local_folder": LocalFolderConnector(),
    "google_drive": NotConfiguredConnector("google_drive", "Google Drive", "Import qualification documents from a shared drive (OAuth required)."),
    "erp": NotConfiguredConnector("erp", "ERP database", "Pull audited financials and project records from your ERP (read-only credentials required)."),
    "dms": NotConfiguredConnector("dms", "Document repository", "SharePoint / DMS document sync (authorised integration required)."),
}
