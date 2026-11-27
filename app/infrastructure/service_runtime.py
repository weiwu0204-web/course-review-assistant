"""Build process-local database pools and private storage clients."""

from app.infrastructure.database import Database
from app.infrastructure.object_store import LocalObjects, S3Objects
from app.infrastructure.service_config import ServiceSettings


def make_database(settings: ServiceSettings) -> Database:
    return Database(settings.database_url.get_secret_value())


def make_objects(settings: ServiceSettings):
    if settings.object_backend == "local":
        return LocalObjects(settings.local_objects)

    import boto3
    from botocore.config import Config

    options = {}
    if settings.s3_access_key and settings.s3_secret_key:
        options["aws_access_key_id"] = settings.s3_access_key.get_secret_value()
        options["aws_secret_access_key"] = settings.s3_secret_key.get_secret_value()
    if settings.s3_endpoint and not options:
        raise ValueError("Custom object storage endpoints require explicit service credentials")
    return S3Objects(boto3.client(
        "s3", endpoint_url=settings.s3_endpoint, region_name=settings.s3_region,
        config=Config(connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 3},
                      s3={"addressing_style": "path"} if settings.s3_endpoint else {}),
        **options,
    ), settings.s3_bucket)
