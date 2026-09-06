"""Environment configuration, validated once at import.

Everything CORTEX needs comes from the environment -- there are no config files.
Settings are read and checked at startup rather than at first use, because the
alternative is discovering a missing API key on the user's first message, after
the containers have already reported themselves healthy. That is the worst
version of this failure and it is entirely avoidable.
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("cortex.config")


class ConfigError(RuntimeError):
    """Raised when the environment is missing something the process cannot run without.

    Carries the name of the offending variable, because an error that says
    "configuration invalid" and nothing else costs the reader ten minutes.
    """


def _require(name: str) -> str:
    """Read a required environment variable or fail naming it.

    Returns the value so callers can assign directly; raises ConfigError rather
    than KeyError so the failure surfaces as a configuration problem instead of
    an unhandled lookup deep in a stack trace.
    """
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


@dataclass(frozen=True)
class Settings:
    """Every knob the API reads, resolved once.

    Frozen because configuration changing under a running process is never
    intentional and always confusing.
    """

    surreal_url: str
    surreal_http: str
    namespace: str
    database: str
    root_user: str
    root_pass: str
    viewer_user: str
    viewer_pass: str
    openai_api_key: str
    openai_base_url: str | None
    llm_model: str
    llm_model_fast: str
    embed_model: str
    embed_dim: int

    async def verify_models(self) -> None:
        """Assert every configured model exists on the account, at startup.

        A misspelled or unavailable model currently surfaces as a failed turn on
        someone's first message, long after the containers reported healthy --
        the same class of failure the EMBED_DIM guard already prevents in the
        migration, and just as avoidable.

        A listing that cannot be fetched is a warning, not a failure: an
        OPENAI_BASE_URL pointing at Ollama or vLLM may not implement /v1/models,
        and refusing to start against a working endpoint would be worse than not
        checking.
        """
        import httpx

        base = (self.openai_base_url or "https://api.openai.com/v1").rstrip("/")
        wanted = {self.llm_model, self.llm_model_fast, self.embed_model}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{base}/models",
                    headers={"Authorization": f"Bearer {self.openai_api_key}"},
                )
                response.raise_for_status()
                available = {item["id"] for item in response.json().get("data", [])}
        except Exception as error:  # noqa: BLE001 - an unlistable endpoint is not a failure
            logger.warning("could not list models at %s (%s); skipping the check", base, error)
            return

        missing = sorted(name for name in wanted if name not in available)
        if missing:
            raise ConfigError(
                f"these models are not available on this account: {', '.join(missing)}. "
                f"Set LLM_MODEL / LLM_MODEL_FAST / EMBED_MODEL in .env to models you have."
            )
        logger.info("models verified: %s", ", ".join(sorted(wanted)))

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from os.environ, applying the documented defaults.

        Only OPENAI_API_KEY is genuinely required -- everything else has a
        working default that matches docker-compose.yml, so a developer who
        forgets a variable gets the demo rather than a stack trace.
        """
        # An *empty* OPENAI_BASE_URL is worse than an absent one. The OpenAI SDK
        # reads that variable itself, and an empty string overrides its default
        # with a relative URL -- every call then fails as
        # "Request URL is missing an 'http://' or 'https://' protocol", wrapped
        # in a generic APIConnectionError that points at the network instead.
        #
        # docker-compose passes `${OPENAI_BASE_URL:-}`, which is exactly this
        # case, so the variable is removed rather than merely ignored here.
        if not os.environ.get("OPENAI_BASE_URL", "").strip():
            os.environ.pop("OPENAI_BASE_URL", None)

        surreal_url = os.environ.get("SURREAL_URL", "ws://surrealdb:8000/rpc")
        return cls(
            surreal_url=surreal_url,
            # The same server over HTTP. Derived rather than configured
            # separately, so the two can never drift apart and point at
            # different databases.
            surreal_http=(
                surreal_url.replace("ws://", "http://")
                .replace("wss://", "https://")
                .removesuffix("/rpc")
            ),
            namespace=os.environ.get("SURREAL_NS", "cortex"),
            database=os.environ.get("SURREAL_DB", "main"),
            root_user=os.environ.get("SURREAL_ROOT_USER", "root"),
            root_pass=os.environ.get("SURREAL_ROOT_PASS", "root"),
            viewer_user=os.environ.get("SURREAL_VIEWER_USER", "cortex_viewer"),
            viewer_pass=os.environ.get("SURREAL_VIEWER_PASS", "viewer"),
            openai_api_key=_require("OPENAI_API_KEY"),
            openai_base_url=os.environ.get("OPENAI_BASE_URL") or None,
            llm_model=os.environ.get("LLM_MODEL", "gpt-5.6-terra"),
            llm_model_fast=os.environ.get("LLM_MODEL_FAST", "gpt-5.6-luna"),
            embed_model=os.environ.get("EMBED_MODEL", "text-embedding-3-small"),
            embed_dim=int(os.environ.get("EMBED_DIM", "1536")),
        )
