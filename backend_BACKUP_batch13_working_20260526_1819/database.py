import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError

# Set up logging for error tracking
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# This is the internal Kubernetes DNS for your PostgreSQL pod.
# We use an environment variable so you can override it for local testing.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@postgres-svc.gpu-rental-system.svc.cluster.local:5432/gpu_rental"
)

try:
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()
    logger.info("Database engine initialized successfully.")
except SQLAlchemyError as e:
    logger.critical(f"FATAL: Failed to initialize database engine. Error: {e}")
    raise

def get_db():
    """Dependency injection for database sessions with strict error handling."""
    db = SessionLocal()
    try:
        yield db
    except SQLAlchemyError as e:
        logger.error(f"Database session error occurred: {e}")
        db.rollback()
        raise
    finally:
        db.close()
