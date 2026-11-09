"""Bounded Cohere v2 / DashScope rerank adapter with strict index mapping."""

import math

import httpx

from backend.config import Settings
from backend.models.model_settings import ConnectionVersion, ModelProfile
from backend.rag.documents import IngestError
from backend.services.model_probe import Attempt, ModelProbe, ProbeFailure


class Rerank:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings, self.transport = settings, transport

    async def rank(
        self,
        profile: ModelProfile,
        connection: ConnectionVersion,
        query: str,
        documents: list[str],
        usage: Attempt,
    ) -> list[tuple[int, float]]:
        if (
            profile.provider_protocol not in {"cohere-rerank-v2", "dashscope-rerank"}
            or connection.provider_protocol != profile.provider_protocol
            or connection.base_url != profile.base_url
            or profile.base_url
            not in self.settings.model_base_urls.get(profile.provider_protocol, [])
        ):
            raise IngestError("MODEL_CONFIG_INVALID")
        probe = ModelProbe(self.settings, self.transport)
        dashscope = profile.provider_protocol == "dashscope-rerank"
        top = min(8, len(documents))
        payload = (
            {
                "model": profile.model_name,
                "input": {"query": query, "documents": documents},
                "parameters": {"top_n": top, "return_documents": False},
            }
            if dashscope
            else {"model": profile.model_name, "query": query, "documents": documents, "top_n": top}
        )
        try:
            await probe.destination(profile.base_url)
            async with httpx.AsyncClient(
                base_url=profile.base_url + "/",
                headers={"Authorization": f"Bearer {connection.credential_value}"},
                timeout=httpx.Timeout(15, connect=5),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                data = await probe.request(
                    client,
                    "services/rerank/text-rerank/text-rerank" if dashscope else "rerank",
                    payload,
                    usage,
                )
            units = (
                data.get("usage", {}) if dashscope else data.get("meta", {}).get("billed_units", {})
            )
            allowed = {"total_tokens"} if dashscope else {"search_units", "input_tokens"}
            if not isinstance(units, dict) or any(
                type(v) is not int or v < 0 for k, v in units.items() if k in allowed
            ):
                raise IngestError("MODEL_RESPONSE_INVALID")
            usage.billed_units = {k: v for k, v in units.items() if k in allowed}
            rows = data["output"]["results"] if dashscope else data["results"]
            if not isinstance(rows, list) or len(rows) != top:
                raise IngestError("MODEL_RESPONSE_INVALID")
            result: list[tuple[int, float]] = []
            for row in rows:
                index, score = row["index"], row["relevance_score"]
                if (
                    type(index) is not int
                    or not 0 <= index < len(documents)
                    or index in {i for i, _ in result}
                    or type(score) not in {float, int}
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                    or result
                    and score > result[-1][1]
                ):
                    raise IngestError("MODEL_RESPONSE_INVALID")
                result.append((index, float(score)))
            return result
        except ProbeFailure as exc:
            raise IngestError(exc.code) from None
        except (TimeoutError, httpx.TimeoutException):
            raise IngestError("MODEL_TIMEOUT") from None
        except (httpx.HTTPError, OSError):
            raise IngestError("MODEL_UNAVAILABLE") from None
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise IngestError("MODEL_RESPONSE_INVALID") from None
