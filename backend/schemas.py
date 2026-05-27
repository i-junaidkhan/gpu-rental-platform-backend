from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from enum import Enum

class UserRoleEnum(str, Enum):
    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    USER = "user"
    CUSTOMER = "customer"

class PlanTypeEnum(str, Enum):
    MIG = "MIG"
    SHARED = "Shared"

class InstanceStatusEnum(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    DELETED = "deleted"

class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    max_gpu_count: int = 0
    max_storage_gb: int = 0

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    max_gpu_count: Optional[int] = None
    max_storage_gb: Optional[int] = None

class ProjectResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    max_gpu_count: int
    max_storage_gb: int
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class UserCreate(BaseModel):
    username: str
    email: str
    role: UserRoleEnum = UserRoleEnum.USER
    project_id: Optional[int] = None
    mfa_enabled: Optional[bool] = False
    balance: Optional[float] = 0.0

class UserUpdate(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    role: Optional[UserRoleEnum] = None
    project_id: Optional[int] = None
    mfa_enabled: Optional[bool] = None
    balance: Optional[float] = None

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    role: UserRoleEnum
    project_id: Optional[int] = None
    mfa_enabled: bool
    balance: float
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class PlanCreate(BaseModel):
    name: str
    plan_type: PlanTypeEnum
    k8s_resource_name: str
    resource_count: int = 1
    price_per_hour: float

class PlanUpdate(BaseModel):
    name: Optional[str] = None
    plan_type: Optional[PlanTypeEnum] = None
    k8s_resource_name: Optional[str] = None
    resource_count: Optional[int] = None
    price_per_hour: Optional[float] = None

class PlanResponse(BaseModel):
    id: int
    name: str
    plan_type: PlanTypeEnum
    k8s_resource_name: str
    resource_count: int
    price_per_hour: float
    class Config:
        from_attributes = True

class InstanceCreate(BaseModel):
    user_id: int
    project_id: int
    plan_id: int
    image: str
    gpu_id: Optional[int] = None
    app_type: Optional[str] = "terminal"
    cpu_cores: Optional[int] = None
    memory_gb: Optional[int] = None
    shm_gb: Optional[int] = None
    storage_id: Optional[int] = None

class InstanceResponse(BaseModel):
    id: int
    user_id: int
    project_id: int
    plan_id: int
    pod_name: str
    namespace: str
    pvc_name: Optional[str] = None
    status: InstanceStatusEnum
    accumulated_cost: float = 0.0
    app_type: Optional[str] = "terminal"
    image: Optional[str] = None
    cpu_cores: Optional[int] = None
    memory_gb: Optional[int] = None
    shm_gb: Optional[int] = None
    storage_id: Optional[int] = None
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class InstancePortCreate(BaseModel):
    port: int
    target_port: Optional[int] = None
    protocol: Optional[str] = "TCP"

class InstancePortResponse(BaseModel):
    id: int
    instance_id: int
    port: int
    target_port: int
    node_port: Optional[int] = None
    protocol: str
    service_name: str
    launch_url: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class BillingEventResponse(BaseModel):
    id: int
    project_id: Optional[int] = None
    user_id: int
    instance_id: int
    amount: float
    event_type: str
    timestamp: Optional[datetime] = None
    class Config:
        from_attributes = True

class StorageVolumeCreate(BaseModel):
    name: str
    mount_path: str
    total_capacity_gb: int
    storage_class: str
    status: Optional[str] = "available"

class StorageVolumeUpdate(BaseModel):
    name: Optional[str] = None
    mount_path: Optional[str] = None
    total_capacity_gb: Optional[int] = None
    storage_class: Optional[str] = None
    status: Optional[str] = None

class StorageVolumeResponse(BaseModel):
    id: int
    name: str
    mount_path: str
    total_capacity_gb: int
    used_capacity_gb: int
    storage_class: str
    status: str
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class UserStorageCreate(BaseModel):
    project_id: int
    user_id: int
    volume_id: int
    folder_path: Optional[str] = None
    quota_gb: int

class UserStorageResponse(BaseModel):
    id: int
    project_id: int
    user_id: int
    volume_id: int
    folder_path: str
    quota_gb: int
    used_gb: float
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class AllowedImageCreate(BaseModel):
    image_url: str
    display_name: str
    description: Optional[str] = None
    is_public: bool = True
    requires_secret: bool = False
    secret_name: Optional[str] = None

class AllowedImageUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None
    requires_secret: Optional[bool] = None
    secret_name: Optional[str] = None

class AllowedImageResponse(BaseModel):
    id: int
    image_url: str
    display_name: str
    description: Optional[str] = None
    is_public: bool
    requires_secret: bool
    secret_name: Optional[str] = None
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class AlertRuleCreate(BaseModel):
    name: str
    metric: str
    threshold: float
    condition: str
    action: str = "none"
    target: Optional[str] = None
    enabled: bool = True

class AlertRuleUpdate(BaseModel):
    name: Optional[str] = None
    metric: Optional[str] = None
    threshold: Optional[float] = None
    condition: Optional[str] = None
    action: Optional[str] = None
    target: Optional[str] = None
    enabled: Optional[bool] = None

class AlertRuleResponse(BaseModel):
    id: int
    name: str
    metric: str
    threshold: float
    condition: str
    action: str
    target: Optional[str] = None
    enabled: bool
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True
