from fastapi import FastAPI

from app.agent.authority_agent import AdkGeminiAuthorityAssessor
from app.agent.intelligence_agent import AdkGeminiIntelligenceAssessor
from app.config import Settings
from app.routes.events import router as events_router
from app.routes.operational import router as operational_router
from app.routes.health import router as health_router
from app.services.firestore_store import FirestoreRunStore, InMemoryRunStore
from app.services.commercegov_client import CommerceGovClient
from app.services.commercegov_credentials import (
    CommerceGovCredentialProvider,
    SecretManagerTokenStore,
)


def create_app(*, store=None, assessor=None, intelligence_assessor=None, settings: Settings | None = None, commercegov_client=None) -> FastAPI:
    settings = settings or Settings.from_environment()
    app = FastAPI(title="CommerceGov Authority Agent", version="0.1.0")
    if store is None:
        if settings.use_in_memory_store:
            store = InMemoryRunStore()
        elif not settings.google_cloud_project:
            # A deliberate local-safe default. Production must provide a project.
            store = InMemoryRunStore()
        else:
            store = FirestoreRunStore(settings.google_cloud_project, settings.firestore_database)
    app.state.run_store = store
    app.state.settings = settings
    app.state.assessor = assessor or AdkGeminiAuthorityAssessor(settings)
    app.state.intelligence_assessor = intelligence_assessor or AdkGeminiIntelligenceAssessor(settings)
    if commercegov_client is None:
        credential_provider = None
        if settings.google_cloud_project and settings.commercegov_api_token:
            credential_provider = CommerceGovCredentialProvider(
                access_token=settings.commercegov_api_token,
                token_store=SecretManagerTokenStore(settings.google_cloud_project),
                access_secret=settings.commercegov_access_secret,
                refresh_secret=settings.commercegov_refresh_secret,
                token_url=settings.commercegov_oauth_token_url,
                client_id=settings.commercegov_oauth_client_id,
            )
        commercegov_client = CommerceGovClient(
            base_url=settings.commercegov_api_url,
            api_token=settings.commercegov_api_token,
            credential_provider=credential_provider,
        )
    app.state.commercegov_client = commercegov_client
    app.include_router(health_router)
    app.include_router(events_router)
    app.include_router(operational_router)
    return app


app = create_app()
