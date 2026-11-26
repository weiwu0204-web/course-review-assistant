"""Production-shaped API factory: uvicorn app.service_main:create_app --factory."""

from app.api.service_routes import create_service_app
from app.courses.service import ReviewService
from app.infrastructure.service_config import ServiceSettings
from app.infrastructure.service_runtime import make_database, make_objects


def create_app():
    settings = ServiceSettings()
    return create_service_app(ReviewService(
        make_database(settings), make_objects(settings),
        upload_limit=settings.upload_limit, session_ttl=settings.session_ttl,
    ), dispose_on_shutdown=True)
