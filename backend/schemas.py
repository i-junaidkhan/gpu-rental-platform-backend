from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    max_gpu_count: Optional[int] = 0
    max_storage_gb: Optional[int] = 0


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
    created_at: datetime

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str
    email: str
    role: Optional[str] = "user"
    balance: Optional[float] = 0.0
    project_id: Optional[int] = None
    mfa_enabled: Optional[bool] = False


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    balance: float
    project_id: Optional[int] = None
    mfa_enabled: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class PlanCreate(BaseModel):
    name: str
    plan_type: str
    k8s_resource_name: str
    resource_count: int = 1
    price_per_hour: float


class PlanResponse(BaseModel):
    id: int
    name: str
    plan_type: str
    k8s_resource_name: str
    resource_count: int
    price_per_hour: float

    class Config:
        from_attributes = True

class InstanceCreate(BaseModel):
    user_id: int
    plan_id: int
    gpu_id: Optional[int] = None  # Frontend sends this, we will ignore it for now
    image: str

class InstanceResponse(BaseModel):
    id: int
    user_id: int
    plan_id: int
    pod_name: str
    status: str

    class Config:
        from_attributes = True

# ============== STORAGE ==============

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
    user_id: int
    volume_id: int
    folder_path: str
    quota_gb: int


class UserStorageResponse(BaseModel):
    id: int
    user_id: int
    volume_id: int
    folder_path: str
    quota_gb: int
    used_gb: float
    created_at: datetime

    class Config:
        from_attributes = True
