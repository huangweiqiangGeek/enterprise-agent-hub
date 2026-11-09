import uvicorn

from backend.config import Settings
from backend.runtime import run_async


def main() -> None:
    settings = Settings()
    config = uvicorn.Config(
        "backend.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    run_async(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
