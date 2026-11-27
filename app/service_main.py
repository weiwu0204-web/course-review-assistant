"""Production-shaped API factory: uvicorn app.service_main:create_app --factory."""

from app.api.service_routes import create_service_app
from app.courses.service import ReviewService
from app.infrastructure.service_config import ServiceSettings
from app.infrastructure.service_runtime import make_database, make_objects
from app.study.service import StudyService


def create_app():
    settings = ServiceSettings()
    model = settings.model_settings()
    if settings.generator == "model" and model is None:
        raise ValueError("Model mode requires service model configuration")
    review = ReviewService(
        make_database(settings), make_objects(settings),
        upload_limit=settings.upload_limit, session_ttl=settings.session_ttl,
    )
    return create_service_app(review, study=StudyService(review, model), dispose_on_shutdown=True,
                              repair_allowed=settings.repair_pricing() is not None)
