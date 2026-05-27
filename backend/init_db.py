import logging
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from database import engine
from models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_IMAGES = [
    {
        "image_url": "docker.io/library/ubuntu:22.04",
        "display_name": "Ubuntu 22.04",
        "description": "Base Ubuntu image",
        "is_public": True,
        "requires_secret": False,
        "secret_name": None,
    },
    {
        "image_url": "docker.io/jupyter/minimal-notebook:latest",
        "display_name": "Jupyter Minimal Notebook",
        "description": "Known working JupyterLab notebook image",
        "is_public": True,
        "requires_secret": False,
        "secret_name": None,
    },
    {
        "image_url": "jupyter/minimal-notebook:latest",
        "display_name": "Jupyter Minimal Notebook Short Alias",
        "description": "Short-name compatibility alias for frontend/API",
        "is_public": True,
        "requires_secret": False,
        "secret_name": None,
    },
    {
        "image_url": "docker.io/nvidia/cuda:12.0-base-ubuntu22.04",
        "display_name": "NVIDIA CUDA 12 base",
        "description": "CUDA 12 base image",
        "is_public": True,
        "requires_secret": False,
        "secret_name": None,
    },
    {
        "image_url": "docker.io/nvidia/cuda:11.8-base-ubuntu22.04",
        "display_name": "NVIDIA CUDA 11.8 base",
        "description": "CUDA 11.8 base image",
        "is_public": True,
        "requires_secret": False,
        "secret_name": None,
    },
    {
        "image_url": "docker.io/pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime",
        "display_name": "PyTorch CUDA runtime",
        "description": "PyTorch GPU runtime image",
        "is_public": True,
        "requires_secret": False,
        "secret_name": None,
    },
    {
        "image_url": "docker.io/tensorflow/tensorflow:2.13.0-gpu",
        "display_name": "TensorFlow GPU",
        "description": "TensorFlow GPU image",
        "is_public": True,
        "requires_secret": False,
        "secret_name": None,
    },
]


def run_safe_migrations(connection):
    logger.info("Running safe batch14 modular migrations...")

    # ------------------------------------------------------------------
    # instances: runtime memory fields for restart/start correctness
    # ------------------------------------------------------------------
    connection.execute(text(
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS app_type VARCHAR DEFAULT 'terminal' NOT NULL"
    ))
    connection.execute(text(
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS image VARCHAR DEFAULT 'docker.io/library/ubuntu:22.04' NOT NULL"
    ))
    connection.execute(text("ALTER TABLE instances ADD COLUMN IF NOT EXISTS cpu_cores INTEGER"))
    connection.execute(text("ALTER TABLE instances ADD COLUMN IF NOT EXISTS memory_gb INTEGER"))
    connection.execute(text("ALTER TABLE instances ADD COLUMN IF NOT EXISTS shm_gb INTEGER"))
    connection.execute(text("ALTER TABLE instances ADD COLUMN IF NOT EXISTS storage_id INTEGER"))
    connection.execute(text(
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS accumulated_cost DOUBLE PRECISION DEFAULT 0"
    ))

    connection.execute(text("UPDATE instances SET app_type = 'terminal' WHERE app_type IS NULL"))
    connection.execute(text(
        "UPDATE instances SET image = 'docker.io/library/ubuntu:22.04' WHERE image IS NULL"
    ))
    connection.execute(text("UPDATE instances SET accumulated_cost = 0 WHERE accumulated_cost IS NULL"))

    # Preserve known tested Jupyter instance restart behavior.
    connection.execute(text("""
        UPDATE instances
        SET
            app_type = 'jupyter',
            image = 'docker.io/jupyter/minimal-notebook:latest',
            storage_id = COALESCE(storage_id, 3)
        WHERE id = 12
    """))

    # ------------------------------------------------------------------
    # instance_ports: NodePort/app launch fields
    # ------------------------------------------------------------------
    connection.execute(text("ALTER TABLE instance_ports ADD COLUMN IF NOT EXISTS target_port INTEGER"))
    connection.execute(text("UPDATE instance_ports SET target_port = port WHERE target_port IS NULL"))
    connection.execute(text("ALTER TABLE instance_ports ADD COLUMN IF NOT EXISTS node_port INTEGER"))
    connection.execute(text(
        "ALTER TABLE instance_ports ADD COLUMN IF NOT EXISTS protocol VARCHAR DEFAULT 'TCP' NOT NULL"
    ))
    connection.execute(text(
        "ALTER TABLE instance_ports ADD COLUMN IF NOT EXISTS service_name VARCHAR DEFAULT 'unknown-service' NOT NULL"
    ))
    connection.execute(text("ALTER TABLE instance_ports ADD COLUMN IF NOT EXISTS launch_url VARCHAR"))
    connection.execute(text(
        "ALTER TABLE instance_ports ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'open' NOT NULL"
    ))

    # ------------------------------------------------------------------
    # billing_events
    # ------------------------------------------------------------------
    connection.execute(text("ALTER TABLE billing_events ADD COLUMN IF NOT EXISTS project_id INTEGER"))
    connection.execute(text("""
        UPDATE billing_events be
        SET project_id = i.project_id
        FROM instances i
        WHERE be.instance_id = i.id
          AND be.project_id IS NULL
    """))

    # ------------------------------------------------------------------
    # allowed_images safe migration.
    #
    # IMPORTANT:
    # Existing DB has an old allowed_images table with legacy NOT NULL
    # column image_name. We must preserve/fill it.
    # ------------------------------------------------------------------
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS allowed_images (
            id SERIAL PRIMARY KEY
        )
    """))

    # Legacy compatibility column. Existing table may already have this as NOT NULL.
    connection.execute(text("ALTER TABLE allowed_images ADD COLUMN IF NOT EXISTS image_name VARCHAR"))
    connection.execute(text("ALTER TABLE allowed_images ADD COLUMN IF NOT EXISTS image_url VARCHAR"))
    connection.execute(text("ALTER TABLE allowed_images ADD COLUMN IF NOT EXISTS display_name VARCHAR"))
    connection.execute(text("ALTER TABLE allowed_images ADD COLUMN IF NOT EXISTS description TEXT"))
    connection.execute(text("ALTER TABLE allowed_images ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT TRUE"))
    connection.execute(text("ALTER TABLE allowed_images ADD COLUMN IF NOT EXISTS requires_secret BOOLEAN DEFAULT FALSE"))
    connection.execute(text("ALTER TABLE allowed_images ADD COLUMN IF NOT EXISTS secret_name VARCHAR"))
    connection.execute(text("ALTER TABLE allowed_images ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()"))

    # Backfill old/new columns both ways.
    connection.execute(text("""
        UPDATE allowed_images
        SET image_url = COALESCE(image_url, image_name, display_name)
        WHERE image_url IS NULL
    """))

    connection.execute(text("""
        UPDATE allowed_images
        SET image_name = COALESCE(image_name, image_url, display_name)
        WHERE image_name IS NULL
    """))

    connection.execute(text("""
        UPDATE allowed_images
        SET display_name = COALESCE(display_name, image_name, image_url)
        WHERE display_name IS NULL
    """))

    connection.execute(text("""
        UPDATE allowed_images
        SET is_public = TRUE
        WHERE is_public IS NULL
    """))

    connection.execute(text("""
        UPDATE allowed_images
        SET requires_secret = FALSE
        WHERE requires_secret IS NULL
    """))

    # Unique index for new code. Partial index avoids null issues.
    connection.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_allowed_images_image_url
        ON allowed_images (image_url)
        WHERE image_url IS NOT NULL
    """))

    # Insert defaults. Fill BOTH image_name and image_url for old-schema compatibility.
    for img in DEFAULT_IMAGES:
        connection.execute(text("""
            INSERT INTO allowed_images
                (
                    image_name,
                    image_url,
                    display_name,
                    description,
                    is_public,
                    requires_secret,
                    secret_name
                )
            SELECT
                :image_url,
                :image_url,
                :display_name,
                :description,
                :is_public,
                :requires_secret,
                :secret_name
            WHERE NOT EXISTS (
                SELECT 1
                FROM allowed_images
                WHERE image_url = :image_url
                   OR image_name = :image_url
            )
        """), img)

    # ------------------------------------------------------------------
    # alert_rules safe migration
    # ------------------------------------------------------------------
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS alert_rules (
            id SERIAL PRIMARY KEY
        )
    """))

    connection.execute(text("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS name VARCHAR"))
    connection.execute(text("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS metric VARCHAR"))
    connection.execute(text("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS threshold DOUBLE PRECISION"))
    connection.execute(text("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS condition VARCHAR"))
    connection.execute(text("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS action VARCHAR"))
    connection.execute(text("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS target VARCHAR"))
    connection.execute(text("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE"))
    connection.execute(text("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()"))

    logger.info("Safe batch14 modular migrations complete.")


def init_db():
    try:
        logger.info("Attempting to create/verify database tables...")
        Base.metadata.create_all(bind=engine)

        with engine.begin() as connection:
            run_safe_migrations(connection)

        logger.info("Database tables and safe migrations completed successfully.")

    except SQLAlchemyError as e:
        logger.critical(f"FATAL: Database initialization failed. Error: {e}")
        raise

    except Exception as e:
        logger.critical(f"FATAL: Unexpected database initialization error: {e}")
        raise


if __name__ == "__main__":
    init_db()