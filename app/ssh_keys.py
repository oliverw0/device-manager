"""Manages the host's own SSH keypair.

The host acts as an SSH client when bridging a browser terminal to a device.
It holds one dedicated keypair (persisted in the data volume); each device's
chosen user gets the *public* key in their authorized_keys (done by the client
install script), so no passwords ever pass through the web app.
"""
from pathlib import Path

import asyncssh

from .config import settings


def _ssh_dir() -> Path:
    path = settings.data_dir() / "ssh"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_host_keypair() -> tuple[Path, Path]:
    ssh_dir = _ssh_dir()
    private_path = ssh_dir / "id_ed25519"
    public_path = ssh_dir / "id_ed25519.pub"

    if not private_path.exists():
        key = asyncssh.generate_private_key("ssh-ed25519", comment="devicemanager-host")
        private_path.write_bytes(key.export_private_key())
        public_path.write_bytes(key.export_public_key())
        private_path.chmod(0o600)

    return private_path, public_path


def public_key_text() -> str:
    _, public_path = ensure_host_keypair()
    return public_path.read_text().strip()


def private_key_path() -> str:
    private_path, _ = ensure_host_keypair()
    return str(private_path)
