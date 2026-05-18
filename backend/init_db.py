import logging
from sqlalchemy.exc import SQLAlchemyError
from database import engine
from models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def init_db():
    try:
        logger.info("Attempting to create database tables...")
        # Bind the engine and create all tables defined in models.py
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables created successfully.")
    except SQLAlchemyError as e:
        logger.critical(f"FATAL: Database initialization failed. Check your connection string and K8s DNS. Error: {e}")
        raise
    except Exception as e:
        logger.critical(f"FATAL: An unexpected error occurred: {e}")
        raise

if __name__ == "__main__":
    init_db()
