"""
Prints exactly which database host/port your config is resolving to right
now, with the password redacted. Run this FIRST whenever something looks
like it's talking to the wrong database — it rules out .env-loading issues
in about two seconds.

    python scripts/print_config.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from src.config import get_settings
from src.db import _migration_url


def _redact(url: str) -> str:
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", url)


def main() -> None:
    settings = get_settings()
    env_file = Path(".env")

    print(f".env file found at {env_file.resolve()}: {env_file.exists()}")
    if env_file.exists():
        first_bytes = env_file.read_bytes()[:3]
        if first_bytes == b"\xef\xbb\xbf":
            print("  WARNING: .env starts with a UTF-8 BOM — this can make the "
                  "first variable in the file silently fail to load. Re-save "
                  "the file as UTF-8 *without* BOM (e.g. `Set-Content -Encoding utf8` "
                  "in PowerShell, or VS Code's 'Save with Encoding' -> UTF-8).")

    print(f"ENVIRONMENT       = {settings.environment}")
    print(f"DATABASE_URL      = {_redact(settings.database_url)}")
    print(f"MIGRATION URL     = {_redact(_migration_url())}")

    if "localhost" in settings.database_url or "127.0.0.1" in settings.database_url:
        print(
            "\n>>> This is still pointing at localhost, not Supabase. "
            "Either .env isn't being read (see BOM warning above, or check "
            "you're running this command from the project root where .env "
            "lives), or .env's DATABASE_URL genuinely still says localhost."
        )


if __name__ == "__main__":
    main()