from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.model_schemas import Limits


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EAH_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://eah:eah_dev@127.0.0.1:5432/eah"
    redis_url: str = "redis://127.0.0.1:6379/0"
    api_host: str = "127.0.0.1"
    api_port: int = 8300
    browser_origins: list[str] = ["http://127.0.0.1:3300", "http://localhost:3300"]
    cookie_secure: bool = False
    model_base_urls: dict[str, list[str]] = {
        "openai-chat": ["https://api.openai.com/v1"],
        "dashscope-chat": ["https://dashscope.aliyuncs.com/compatible-mode/v1"],
        "openai-embeddings": [
            "https://api.openai.com/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ],
        "cohere-rerank-v2": ["https://api.cohere.com/v2"],
        "dashscope-rerank": ["https://dashscope.aliyuncs.com/api/v1"],
    }
    chat_tokenizers: dict[str, str] = {
        "gpt-4o-mini-2024-07-18": "o200k_base",
        "gpt-4o-2024-08-06": "o200k_base",
        "qwen-plus": "dashscope-qwen-1.26.7",
    }
    system_limits: Limits = Limits()
    # Local SeaweedFS is for development; production supplies its own private bucket.
    s3_endpoint: str = "http://127.0.0.1:4566"
    s3_region: str = "us-east-1"
    s3_bucket: str = "eah-development"
    s3_access_key: str = "test"
    s3_secret_key: str = "test"
    s3_prefix: str = "eah/uploads/"
    max_documents_per_organization: int = Field(default=1000, ge=1, le=1000)
    max_chunks_per_organization: int = Field(default=50000, ge=1, le=50000)
    max_runs_per_organization: int = Field(default=10000, ge=1, le=10000)
    ingestion_queue: str = "eah.ingestion"
    run_queue: str = "eah.runs"
