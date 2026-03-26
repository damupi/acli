import json
import sys
from pathlib import Path

CREDS_PATH = Path.home() / ".config" / "atl" / "credentials"


def load_credentials() -> dict:
    """Load stored credentials or exit with a helpful error."""
    if not CREDS_PATH.exists():
        print("No credentials found. Run: atl auth setup", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(CREDS_PATH.read_text())
    except Exception:
        print("Credentials file is invalid. Run: atl auth setup", file=sys.stderr)
        sys.exit(1)
    if not data.get("email") or not data.get("token") or not data.get("domain"):
        print("Credentials incomplete. Run: atl auth setup", file=sys.stderr)
        sys.exit(1)
    return data


def save_credentials(email: str, token: str, domain: str) -> None:
    """Save credentials to disk as JSON."""
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDS_PATH.write_text(json.dumps({
        "email": email,
        "token": token,
        "domain": domain,
    }, indent=2))
