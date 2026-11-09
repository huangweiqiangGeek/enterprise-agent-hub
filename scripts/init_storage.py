"""Explicitly initialize the configured S3 bucket; application startup never creates it."""

from backend.config import Settings
from backend.runtime import run_async
from backend.services.storage import Storage

if __name__ == "__main__":
    run_async(Storage(Settings()).initialize())
    print("PASS configured S3 bucket initialized")
