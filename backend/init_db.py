import logging
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text
from database import engine
from models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_safe_projects_migration(connection):
    """
    Minimal backward-compatible migration for project management.
    This does not rewrite old instance/billing/storage tables. It only adds
    the projects table plus optional user fields required by /api/projects and users.
    """
    logger.info("Running safe projects migration...")

    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            name VARCHAR UNIQUE NOT NULL,
            description VARCHAR NULL,
            max_gpu_count INTEGER DEFAULT 0,
            max_storage_gb INTEGER DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """))

    connection.execute(text("""
        INSERT INTO projects (name, description, max_gpu_count, max_storage_gb)
        VALUES ('default-project', 'Default project for existing users and resources', 0, 0)
        ON CONFLICT (name) DO NOTHING
    """))

    connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS project_id INTEGER"))
    connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN DEFAULT FALSE NOT NULL"))

    connection.execute(text("""
        UPDATE users
        SET project_id = (SELECT id FROM projects WHERE name = 'default-project' LIMIT 1)
        WHERE project_id IS NULL
    """))
    
    # Phase 2A: Safely bind existing instances to default-project
    connection.execute(text("ALTER TABLE instances ADD COLUMN IF NOT EXISTS project_id INTEGER"))
    connection.execute(text("""
        UPDATE instances 
        SET project_id = (SELECT id FROM projects WHERE name = 'default-project' LIMIT 1) 
        WHERE project_id IS NULL
    """))

    logger.info("Safe projects migration complete.")


def init_db():
    try:
        logger.info("Attempting to create/verify database tables...")
        Base.metadata.create_all(bind=engine)
        with engine.begin() as connection:
            run_safe_projects_migration(connection)
        logger.info("Database tables and safe migrations completed successfully.")
    except SQLAlchemyError as e:
        logger.critical(f"FATAL: Database initialization failed. Check your connection string and K8s DNS. Error: {e}")
        raise
    except Exception as e:
        logger.critical(f"FATAL: An unexpected error occurred: {e}")
        raise


if __name__ == "__main__":
    init_db()
