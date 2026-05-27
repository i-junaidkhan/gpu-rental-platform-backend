import logging
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session
from database import get_db
from api.core_crud import router as core_router
from api.instances import router as instances_router
from api.monitoring import router as monitoring_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="GPU Rental API", version="14-core-modular")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        db_status = "error"
    return {"status": "healthy", "database": db_status}

@app.get("/api/tables")
def list_tables(db: Session = Depends(get_db)):
    result = db.execute(text("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
    """))
    return {"tables": [row[0] for row in result.fetchall()]}

app.include_router(core_router)
app.include_router(instances_router)
app.include_router(monitoring_router)
