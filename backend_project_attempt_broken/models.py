import logging
import enum
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.exc import SQLAlchemyError
from database import Base

logger = logging.getLogger(__name__)

# --- ENUMS ---
class PlanTypeEnum(str, enum.Enum):
    MIG = "MIG"
    SHARED = "Shared"

class InstanceStatusEnum(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    DELETED = "deleted"

class UserRoleEnum(str, enum.Enum):
    SUPERADMIN = "superadmin"
    ADMIN = "admin"  # Project Admin
    USER = "user"

# --- MODELS ---
class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    
    # Resource Quotas assigned to the project
    max_gpu_count = Column(Integer, default=0)
    max_storage_gb = Column(Integer, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    users = relationship("User", back_populates="project")
    instances = relationship("Instance", back_populates="project")
    billing_events = relationship("BillingEvent", back_populates="project")
    project_storages = relationship("UserStorage", back_populates="project")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    
    # Security & Roles
    role = Column(Enum(UserRoleEnum), default=UserRoleEnum.USER, nullable=False)
    mfa_enabled = Column(Boolean, default=False)
    
    # Hierarchy - Users belong to a project (Superadmins might have this as NULL)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    
    balance = Column(Float, default=0.0) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    project = relationship("Project", back_populates="users")
    instances = relationship("Instance", back_populates="owner")
    billing_events = relationship("BillingEvent", back_populates="user")


class RentalPlan(Base):
    __tablename__ = "rental_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    plan_type = Column(Enum(PlanTypeEnum), nullable=False)
    k8s_resource_name = Column(String, nullable=False)
    resource_count = Column(Integer, default=1)
    price_per_hour = Column(Float, nullable=False)


class Instance(Base):
    __tablename__ = "instances"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False) # Tied to project for quota checks
    plan_id = Column(Integer, ForeignKey("rental_plans.id"), nullable=False)
    
    pod_name = Column(String, unique=True, nullable=False)
    namespace = Column(String, nullable=False, default="gpu-rental-system")
    pvc_name = Column(String, nullable=False) 
    status = Column(Enum(InstanceStatusEnum), default=InstanceStatusEnum.PENDING)
    accumulated_cost = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User", back_populates="instances")
    project = relationship("Project", back_populates="instances")
    plan = relationship("RentalPlan")


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False) # Billing targets projects
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False) # The specific user who triggered it
    instance_id = Column(Integer, ForeignKey("instances.id"), nullable=False)
    
    amount = Column(Float, nullable=False)
    event_type = Column(String, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="billing_events")
    user = relationship("User", back_populates="billing_events")
    instance = relationship("Instance")


class StorageVolume(Base):
    __tablename__ = "storage_volumes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False) 
    mount_path = Column(String, nullable=False) 
    total_capacity_gb = Column(Integer, nullable=False) 
    used_capacity_gb = Column(Integer, default=0)
    storage_class = Column(String, nullable=False)
    status = Column(String, default="available") 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project_allocations = relationship("UserStorage", back_populates="volume", cascade="all, delete-orphan")


class UserStorage(Base):
    __tablename__ = "user_storages"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False) # Storage bounded to project
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False) # The specific user folder
    volume_id = Column(Integer, ForeignKey("storage_volumes.id"), nullable=False)
    
    folder_path = Column(String, nullable=False)
    quota_gb = Column(Integer, nullable=False) 
    used_gb = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="project_storages")
    user = relationship("User")
    volume = relationship("StorageVolume", back_populates="project_allocations")


# --- ERROR HANDLING & INITIALIZATION ---
def initialize_database_schema(engine):
    """
    Safely creates all database tables with strict error handling.
    Prevents silent failures during database migrations.
    """
    try:
        logger.info("Attempting to initialize database schema...")
        Base.metadata.create_all(bind=engine)
        logger.info("Database schema successfully verified and created.")
    except SQLAlchemyError as db_err:
        logger.critical(f"FATAL: Database schema initialization failed. Error: {str(db_err)}")
        raise
    except Exception as e:
        logger.critical(f"FATAL: Unexpected system error during DB init. Error: {str(e)}")
        raise