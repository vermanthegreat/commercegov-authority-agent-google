from fastapi import FastAPI

from app.agent.authority_agent import AdkGeminiAuthorityAssessor
from app.config import Settings
from app.routes.events import router as events_router
from app.routes.health import router as health_router
from app.services.firestore_store import FirestoreRunStore, InMemoryRunStore
from app.services.commercegov_client import CommerceGovClient


def create_app(*, store=None, assessor=None, settings: Settings | None = None, commercegov_client=None) -> FastAPI:
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
    app.state.commercegov_client = commercegov_client or CommerceGovClient(
        base_url=settings.commercegov_api_url,
        api_token=settings.commercegov_api_token
    )
    app.include_router(health_router)
    app.include_router(events_router)
    return app


app = create_app()
