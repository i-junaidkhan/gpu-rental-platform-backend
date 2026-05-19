import logging
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.exc import SQLAlchemyError
from database import Base
import enum

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

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    role = Column(String, default="user")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    mfa_enabled = Column(Boolean, default=False, nullable=False)
    balance = Column(Float, default=0.0)  # Required to auto-kill instances when funds run out
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="users")
    instances = relationship("Instance", back_populates="owner", cascade="all, delete-orphan")
    billing_events = relationship("BillingEvent", back_populates="user", cascade="all, delete-orphan")

class RentalPlan(Base):
    __tablename__ = "rental_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)  # e.g., "A100 MIG 10GB"
    plan_type = Column(Enum(PlanTypeEnum), nullable=False)
    k8s_resource_name = Column(String, nullable=False)  # MUST be 'nvidia.com/mig-1g.10gb' or 'nvidia.com/gpu'
    resource_count = Column(Integer, default=1)
    price_per_hour = Column(Float, nullable=False)

class Instance(Base):
    __tablename__ = "instances"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True) 
    # (Nullable=True locally so DB migration doesn't crash, but enforced in API)
    plan_id = Column(Integer, ForeignKey("rental_plans.id"), nullable=False)
    pod_name = Column(String, unique=True, nullable=False)
    namespace = Column(String, nullable=False, default="gpu-rental-system")
    pvc_name = Column(String, nullable=False)  # Maps to the kf-work1 NAS storage
    status = Column(Enum(InstanceStatusEnum), default=InstanceStatusEnum.PENDING)
    accumulated_cost = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User", back_populates="instances")
    project = relationship("Project", back_populates="instances")
    plan = relationship("RentalPlan")

class BillingEvent(Base):
    __tablename__ = "billing_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    instance_id = Column(Integer, ForeignKey("instances.id"), nullable=False)
    amount = Column(Float, nullable=False)
    event_type = Column(String, nullable=False)  # e.g., "minute_charge", "refund"
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="billing_events")
    instance = relationship("Instance")


class StorageVolume(Base):
    __tablename__ = "storage_volumes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)  # e.g., "data3-main"
    mount_path = Column(String, nullable=False)  # e.g., "/data3"
    total_capacity_gb = Column(Integer, nullable=False)  # e.g., 10000 (10TB)
    used_capacity_gb = Column(Integer, default=0)
    storage_class = Column(String, nullable=False)  # e.g., "kf-work1"
    status = Column(String, default="available")  # available, full, maintenance
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user_storages = relationship("UserStorage", back_populates="volume", cascade="all, delete-orphan")


class UserStorage(Base):
    __tablename__ = "user_storages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    volume_id = Column(Integer, ForeignKey("storage_volumes.id"), nullable=False)
    folder_path = Column(String, nullable=False)  # e.g., "/data3/users/user1"
    quota_gb = Column(Integer, nullable=False)  # Max storage for this user
    used_gb = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")
    volume = relationship("StorageVolume", back_populates="user_storages")
