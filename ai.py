"""Microsoft Foundry clients, keyless. AI is on when AZURE_OPENAI_ENDPOINT is set.

Deployments (env overrides): AZURE_OPENAI_DEPLOYMENT=gpt-4.1-mini (analyst, narration, translation),
FOUNDRY_ROUTER_DEPLOYMENT=phi-4-mini (router), FOUNDRY_EMBED_DEPLOYMENT=embed-small (memory salience).
"""
import os
from functools import lru_cache

SCOPE = "https://cognitiveservices.azure.com/.default"
API_VERSION = "2024-10-21"


def enabled():
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT"))


def endpoint():
    return os.environ["AZURE_OPENAI_ENDPOINT"]


def deployment(kind):
    return {"chat": os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini"),
            "router": os.getenv("FOUNDRY_ROUTER_DEPLOYMENT", "phi-4-mini"),
            "embed": os.getenv("FOUNDRY_EMBED_DEPLOYMENT", "embed-small")}[kind]


@lru_cache(maxsize=1)
def credential():
    """Managed identity inside Azure (Container Apps sets IDENTITY_ENDPOINT), your `az login` elsewhere.
    Avoids DefaultAzureCredential's slow probing on a laptop."""
    from azure.identity import AzureCliCredential, ManagedIdentityCredential
    return ManagedIdentityCredential() if os.getenv("IDENTITY_ENDPOINT") else AzureCliCredential()


@lru_cache(maxsize=1)
def client():
    from azure.identity import get_bearer_token_provider
    from openai import AzureOpenAI
    return AzureOpenAI(azure_endpoint=endpoint(), api_version=API_VERSION, timeout=25, max_retries=1,
                       azure_ad_token_provider=get_bearer_token_provider(credential(), SCOPE))
