"""Shared paths and credentials for every automation job.

One credential file, one loader. A job never reads ~/.automation.env itself.
"""

import os
from pathlib import Path

HOME = Path.home()


def _resolve_vault() -> Path:
    """Find the vault on whichever machine this is running on.

    The box keeps it at ~/vault, the Mac at ~/Obsidian/<VAULT_NAME>.
    The scripts that used to live in _system/scripts/ derived this by walking
    three directories up from their own location, which worked only while they
    sat inside the vault. Moving code out of the vault is the whole point, so
    the path has to be resolved rather than inferred from where the file is.
    """
    override = os.environ.get("VAULT_PATH")
    if override:
        return Path(override)
    for candidate in (HOME / "vault", HOME / "Obsidian" / "<VAULT_NAME>"):
        if (candidate / "_system").is_dir():
            return candidate
    return HOME / "vault"


VAULT = _resolve_vault()
INBOX = VAULT / "Inbox"

STATE_DIR = HOME / ".local" / "state" / "automation"
LOCK_DIR = STATE_DIR / "locks"

ENV_FILE = HOME / ".automation.env"

# Hetzner blocks outbound 25 and 465. A blocked port hangs until timeout
# rather than refusing, which reads exactly like a bad password.
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def load_env() -> dict[str, str]:
    """Read ~/.automation.env. Returns {} when absent, never raises.

    Absent is a normal state: the note channel must keep working when mail
    is unconfigured, so callers degrade rather than fail.
    """
    if not ENV_FILE.exists():
        return {}

    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
