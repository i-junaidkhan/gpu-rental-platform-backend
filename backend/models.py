import enum
import logging
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, Boolean, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.exc import SQLAlchemyError
from database import Base

logger = logging.getLogger(__name__)

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
    ADMIN = "admin"
    USER = "user"
    CUSTOMER = "customer"

class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    max_gpu_count = Column(Integer, default=0)
    max_storage_gb = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    users = relationship("User", back_populates="project")
    instances = relationship("Instance", back_populates="project")
    billing_events = relationship("BillingEvent", back_populates="project")
    project_storages = relationship("UserStorage", back_populates="project")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    role = Column(Enum(UserRoleEnum), default=UserRoleEnum.USER, nullable=False)
    mfa_enabled = Column(Boolean, default=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    balance = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
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
    instances = relationship("Instance", back_populates="plan")

class Instance(Base):
    __tablename__ = "instances"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("rental_plans.id"), nullable=False)
    pod_name = Column(String, unique=True, nullable=False)
    namespace = Column(String, nullable=False, default="gpu-rental-system")
    pvc_name = Column(String, nullable=True)
    status = Column(Enum(InstanceStatusEnum), default=InstanceStatusEnum.PENDING)
    accumulated_cost = Column(Float, default=0.0)
    # Batch13.1: remember runtime config for restart/recreate.
    app_type = Column(String, nullable=False, default="terminal")
    image = Column(String, nullable=False, default="docker.io/library/ubuntu:22.04")
    cpu_cores = Column(Integer, nullable=True)
    memory_gb = Column(Integer, nullable=True)
    shm_gb = Column(Integer, nullable=True)
    storage_id = Column(Integer, ForeignKey("user_storages.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    owner = relationship("User", back_populates="instances")
    project = relationship("Project", back_populates="instances")
    plan = relationship("RentalPlan", back_populates="instances")
    ports = relationship("InstancePort", back_populates="instance")
    billing_events = relationship("BillingEvent", back_populates="instance")

class InstancePort(Base):
    __tablename__ = "instance_ports"
    id = Column(Integer, primary_key=True, index=True)
    instance_id = Column(Integer, ForeignKey("instances.id"), nullable=False)
    port = Column(Integer, nullable=False)
    target_port = Column(Integer, nullable=False)
    node_port = Column(Integer, nullable=True)
    protocol = Column(String, nullable=False, default="TCP")
    service_name = Column(String, nullable=False)
    launch_url = Column(String, nullable=True)
    status = Column(String, nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    instance = relationship("Instance", back_populates="ports")

class BillingEvent(Base):
    __tablename__ = "billing_events"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    instance_id = Column(Integer, ForeignKey("instances.id"), nullable=False)
    amount = Column(Float, nullable=False)
    event_type = Column(String, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    project = relationship("Project", back_populates="billing_events")
    user = relationship("User", back_populates="billing_events")
    instance = relationship("Instance", back_populates="billing_events")

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
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    volume_id = Column(Integer, ForeignKey("storage_volumes.id"), nullable=False)
    folder_path = Column(String, nullable=False)
    quota_gb = Column(Integer, nullable=False)
    used_gb = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    project = relationship("Project", back_populates="project_storages")
    user = relationship("User")
    volume = relationship("StorageVolume", back_populates="project_allocations")

class AllowedImage(Base):
    __tablename__ = "allowed_images"
    id = Column(Integer, primary_key=True, index=True)
    image_url = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    is_public = Column(Boolean, default=True)
    requires_secret = Column(Boolean, default=False)
    secret_name = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AlertRule(Base):
    __tablename__ = "alert_rules"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    metric = Column(String, nullable=False)
    threshold = Column(Float, nullable=False)
    condition = Column(String, nullable=False)  # gt / lt
    action = Column(String, nullable=False)     # email / slack / webhook / none
    target = Column(String, nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

def initialize_database_schema(engine):
    try:
        logger.info("Attempting to initialize database schema...")
        Base.metadata.create_all(bind=engine)
        logger.info("Database schema successfully verified and created.")
    except SQLAlchemyError as db_err:
        logger.critical(f"FATAL: Database schema initialization failed. Error: {str(db_err)}")
        raise
