from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum

# =========================
# ENUMS (Strict Validation)
# =========================
class UserRoleEnum(str, Enum):
    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    USER = "user"

class PlanTypeEnum(str, Enum):
    MIG = "MIG"
    SHARED = "Shared"

class InstanceStatusEnum(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    DELETED = "deleted"

# =========================
# PROJECTS
# =========================
class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    max_gpu_count: int = 0
    max_storage_gb: int = 0

class ProjectResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    max_gpu_count: int
    max_storage_gb: int
    created_at: datetime

    class Config:
        from_attributes = True

# =========================
# USERS
# =========================
class UserCreate(BaseModel):
    username: str
    email: str
    role: UserRoleEnum = UserRoleEnum.USER
    project_id: Optional[int] = None  # Superadmins might not need a project, Users MUST have one
    mfa_enabled: Optional[bool] = False
    balance: Optional[float] = 0.0

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    role: UserRoleEnum
    project_id: Optional[int]
    mfa_enabled: bool
    balance: float
    created_at: datetime

    class Config:
        from_attributes = True

# =========================
# PLANS
# =========================
class PlanCreate(BaseModel):
    name: str
    plan_type: PlanTypeEnum
    k8s_resource_name: str
    resource_count: int = 1
    price_per_hour: float

class PlanResponse(BaseModel):
    id: int
    name: str
    plan_type: PlanTypeEnum
    k8s_resource_name: str
    resource_count: int
    price_per_hour: float

    class Config:
        from_attributes = True

# =========================
# INSTANCES
# =========================
class InstanceCreate(BaseModel):
    user_id: int
    project_id: int  # Mandatory now: Quota is drawn from the project
    plan_id: int
    image: str
    gpu_id: Optional[int] = None  # Ignored for now, kept for frontend compatibility

class InstanceResponse(BaseModel):
    id: int
    user_id: int
    project_id: int
    plan_id: int
    pod_name: str
    namespace: str
    pvc_name: str
    status: InstanceStatusEnum
    accumulated_cost: float
    created_at: datetime

    class Config:
        from_attributes = True

# =========================
# BILLING
# =========================
class BillingEventResponse(BaseModel):
    id: int
    project_id: int
    user_id: int
    instance_id: int
    amount: float
    event_type: str
    timestamp: datetime

    class Config:
        from_attributes = True

# =========================
# STORAGE
# =========================
class StorageVolumeCreate(BaseModel):
    name: str
    mount_path: str
    total_capacity_gb: int
    storage_class: str
    status: Optional[str] = "available"

class StorageVolumeResponse(BaseModel):
    id: int
    name: str
    mount_path: str
    total_capacity_gb: int
    used_capacity_gb: int
    storage_class: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True

class UserStorageCreate(BaseModel):
    project_id: int  # Storage now bounds to the Project's quota limits
    user_id: int
    volume_id: int
    folder_path: str
    quota_gb: int

class UserStorageResponse(BaseModel):
    id: int
    project_id: int
    user_id: int
    volume_id: int
    folder_path: str
    quota_gb: int
    used_gb: float
    created_at: datetime

    class Config:
        from_attributes = True