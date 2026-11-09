"""Bounded embedding wire adapter; immutable connection/profile passed by ingestion."""

import math

import httpx

from backend.config import Settings
from backend.models.model_settings import ConnectionVersion, ModelProfile
from backend.rag.documents import IngestError
from backend.services.model_probe import Attempt, ModelProbe, ProbeFailure, WireUsage


class Embeddings:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings, self.transport = settings, transport

    async def create(
        self, profile: ModelProfile, connection: ConnectionVersion, texts: list[str], usage: Attempt
    ) -> list[list[float]]:
        if (
            profile.provider_protocol != "openai-embeddings"
            or connection.provider_protocol != profile.provider_protocol
            or connection.base_url != profile.base_url
            or profile.base_url not in self.settings.model_base_urls.get("openai-embeddings", [])
            or not profile.dimension
        ):
            raise IngestError("MODEL_CONFIG_INVALID")
        probe = ModelProbe(self.settings, self.transport)
        try:
            await probe.destination(profile.base_url)
            async with httpx.AsyncClient(
                base_url=profile.base_url + "/",
                headers={"Authorization": f"Bearer {connection.credential_value}"},
                timeout=httpx.Timeout(25, connect=10),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                payload = {
                    "model": profile.model_name,
                    "input": texts,
                    "dimensions": profile.dimension,
                    "encoding_format": "float",
                }
                data = await probe.request(client, "embeddings", payload, usage)
            if data.get("usage") is not None:
                counts = WireUsage.model_validate(data["usage"])
                usage.input_tokens, usage.output_tokens = counts.prompt_tokens, 0
                usage.token_source = "actual"
            rows = data["data"]
            if len(rows) != len(texts) or {r["index"] for r in rows} != set(range(len(texts))):
                raise IngestError("MODEL_RESPONSE_INVALID")
            vectors = []
            for row in sorted(rows, key=lambda r: r["index"]):
                values = row["embedding"]
                if (
                    type(row["index"]) is not int
                    or len(values) != profile.dimension
                    or any(type(v) not in {float, int} or not math.isfinite(v) for v in values)
                    or not any(values)
                ):
                    raise IngestError("EMBEDDING_DIMENSION_INVALID")
                vectors.append([float(v) for v in values])
            return vectors
        except ProbeFailure as exc:
            raise IngestError(exc.code) from None
        except (httpx.TimeoutException, TimeoutError):
            raise IngestError("MODEL_TIMEOUT") from None
        except (httpx.HTTPError, OSError):
            raise IngestError("MODEL_UNAVAILABLE") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise IngestError("MODEL_RESPONSE_INVALID") from None
