import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from database import get_db
import models
from schemas import UserCreate, UserUpdate, UserResponse, ProjectCreate, ProjectUpdate, ProjectResponse, PlanCreate, PlanUpdate, PlanResponse, StorageVolumeCreate, StorageVolumeUpdate, StorageVolumeResponse, UserStorageCreate, UserStorageResponse

router = APIRouter(tags=["Core CRUD"])
logger = logging.getLogger(__name__)

def validate_project_exists(db: Session, project_id: int) -> models.Project:
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project: raise HTTPException(status_code=404, detail="Project not found")
    return project

def get_project_storage_usage_gb(db: Session, project_id: int) -> int:
    return int(sum(int(row.quota_gb or 0) for row in db.query(models.UserStorage).filter(models.UserStorage.project_id == project_id).all()))

@router.get("/api/projects", response_model=list[ProjectResponse])
def get_projects(db: Session = Depends(get_db)):
    return db.query(models.Project).order_by(models.Project.id).all()

@router.post("/api/projects", response_model=ProjectResponse)
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    row = models.Project(**project.model_dump())
    try: db.add(row); db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Project creation failed")

@router.get("/api/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: Session = Depends(get_db)):
    return validate_project_exists(db, project_id)

@router.put("/api/projects/{project_id}", response_model=ProjectResponse)
def update_project(project_id: int, payload: ProjectUpdate, db: Session = Depends(get_db)):
    row = validate_project_exists(db, project_id)
    for k, v in payload.model_dump(exclude_unset=True).items(): setattr(row, k, v)
    try: db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Project update failed")

@router.delete("/api/projects/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    row = validate_project_exists(db, project_id)
    if db.query(models.User).filter(models.User.project_id == project_id).first(): raise HTTPException(status_code=400, detail="Cannot delete project while users are assigned")
    try: db.delete(row); db.commit(); return {"message":"Project deleted", "id":project_id}
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Project deletion failed")

@router.get("/api/projects/{project_id}/summary")
def get_project_summary(project_id: int, db: Session = Depends(get_db)):
    project = validate_project_exists(db, project_id)
    users_count = db.query(models.User).filter(models.User.project_id == project_id).count()
    active = db.query(models.Instance).filter(models.Instance.project_id == project_id, models.Instance.status.in_([models.InstanceStatusEnum.RUNNING, models.InstanceStatusEnum.PENDING])).all()
    gpu_usage = 0
    for inst in active:
        plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == inst.plan_id).first()
        if plan: gpu_usage += int(plan.resource_count or 0)
    storage_allocated = get_project_storage_usage_gb(db, project_id)
    return {"project_id":project.id, "project_name":project.name, "max_gpu_count":project.max_gpu_count, "gpu_used":gpu_usage, "gpu_available":None if (project.id == 1 and project.max_gpu_count == 0) else max(project.max_gpu_count - gpu_usage, 0), "max_storage_gb":project.max_storage_gb, "storage_allocated_gb":storage_allocated, "storage_available_gb":None if project.max_storage_gb == 0 else max(project.max_storage_gb - storage_allocated, 0), "users_count":users_count, "active_instances_count":len(active)}

@router.post("/api/users", response_model=UserResponse)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    data = user.model_dump()
    if data.get("project_id") is not None: validate_project_exists(db, data["project_id"])
    row = models.User(**data)
    try: db.add(row); db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="User creation failed")

@router.get("/api/users", response_model=list[UserResponse])
def get_users(project_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.User)
    if project_id is not None: q = q.filter(models.User.project_id == project_id)
    return q.order_by(models.User.id).all()

@router.get("/api/users/{user_id}", response_model=UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db)):
    row = db.query(models.User).filter(models.User.id == user_id).first()
    if not row: raise HTTPException(status_code=404, detail="User not found")
    return row

@router.put("/api/users/{user_id}", response_model=UserResponse)
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db)):
    row = db.query(models.User).filter(models.User.id == user_id).first()
    if not row: raise HTTPException(status_code=404, detail="User not found")
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("project_id") is not None: validate_project_exists(db, updates["project_id"])
    for k,v in updates.items(): setattr(row,k,v)
    try: db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="User update failed")

@router.delete("/api/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)):
    row = db.query(models.User).filter(models.User.id == user_id).first()
    if not row: raise HTTPException(status_code=404, detail="User not found")
    if db.query(models.Instance).filter(models.Instance.user_id == user_id, models.Instance.status.in_([models.InstanceStatusEnum.RUNNING, models.InstanceStatusEnum.PENDING])).first(): raise HTTPException(status_code=400, detail="Cannot delete user with running/pending instances")
    try: db.delete(row); db.commit(); return {"message":"User deleted", "id":user_id}
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="User deletion failed")

@router.post("/api/rental-plans", response_model=PlanResponse)
def create_plan(plan: PlanCreate, db: Session = Depends(get_db)):
    row = models.RentalPlan(**plan.model_dump())
    try: db.add(row); db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Plan creation failed")

@router.get("/api/rental-plans", response_model=list[PlanResponse])
def get_plans(db: Session = Depends(get_db)):
    return db.query(models.RentalPlan).order_by(models.RentalPlan.id).all()

@router.put("/api/rental-plans/{plan_id}", response_model=PlanResponse)
def update_plan(plan_id: int, payload: PlanUpdate, db: Session = Depends(get_db)):
    row = db.query(models.RentalPlan).filter(models.RentalPlan.id == plan_id).first()
    if not row: raise HTTPException(status_code=404, detail="Plan not found")
    for k,v in payload.model_dump(exclude_unset=True).items(): setattr(row,k,v)
    try: db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Plan update failed")

@router.delete("/api/rental-plans/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db)):
    row = db.query(models.RentalPlan).filter(models.RentalPlan.id == plan_id).first()
    if not row: raise HTTPException(status_code=404, detail="Plan not found")
    if db.query(models.Instance).filter(models.Instance.plan_id == plan_id).first(): raise HTTPException(status_code=400, detail="Cannot delete plan with existing instances")
    try: db.delete(row); db.commit(); return {"message":"Plan deleted", "id":plan_id}
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Plan deletion failed")

@router.get("/api/storage-volumes", response_model=list[StorageVolumeResponse])
def get_storage_volumes(db: Session = Depends(get_db)):
    return db.query(models.StorageVolume).order_by(models.StorageVolume.id).all()

@router.post("/api/storage-volumes", response_model=StorageVolumeResponse)
def create_storage_volume(volume: StorageVolumeCreate, db: Session = Depends(get_db)):
    row = models.StorageVolume(**volume.model_dump())
    try: db.add(row); db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Storage volume creation failed")

@router.get("/api/storage-volumes/{volume_id}", response_model=StorageVolumeResponse)
def get_storage_volume(volume_id: int, db: Session = Depends(get_db)):
    row = db.query(models.StorageVolume).filter(models.StorageVolume.id == volume_id).first()
    if not row: raise HTTPException(status_code=404, detail="Storage volume not found")
    return row

@router.put("/api/storage-volumes/{volume_id}", response_model=StorageVolumeResponse)
def update_storage_volume(volume_id: int, payload: StorageVolumeUpdate, db: Session = Depends(get_db)):
    row = db.query(models.StorageVolume).filter(models.StorageVolume.id == volume_id).first()
    if not row: raise HTTPException(status_code=404, detail="Storage volume not found")
    for k,v in payload.model_dump(exclude_unset=True).items(): setattr(row,k,v)
    try: db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Storage volume update failed")

@router.delete("/api/storage-volumes/{volume_id}")
def delete_storage_volume(volume_id: int, db: Session = Depends(get_db)):
    row = db.query(models.StorageVolume).filter(models.StorageVolume.id == volume_id).first()
    if not row: raise HTTPException(status_code=404, detail="Storage volume not found")
    try: db.delete(row); db.commit(); return {"message":"Storage volume deleted", "id":volume_id}
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Storage volume deletion failed")

@router.get("/api/user-storages", response_model=list[UserStorageResponse])
def get_user_storages(project_id: int | None = None, user_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.UserStorage)
    if project_id is not None: q = q.filter(models.UserStorage.project_id == project_id)
    if user_id is not None: q = q.filter(models.UserStorage.user_id == user_id)
    return q.order_by(models.UserStorage.id).all()

@router.get("/api/user-storages/user/{user_id}", response_model=list[UserStorageResponse])
def get_user_storage_by_user(user_id: int, db: Session = Depends(get_db)):
    return db.query(models.UserStorage).filter(models.UserStorage.user_id == user_id).all()

@router.post("/api/user-storages", response_model=UserStorageResponse)
def create_user_storage(storage: UserStorageCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == storage.user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
    project_id = storage.project_id or user.project_id
    if project_id is None: raise HTTPException(status_code=400, detail="User has no project_id")
    project = validate_project_exists(db, project_id)
    if user.project_id != project.id: raise HTTPException(status_code=403, detail="User does not belong to selected project")
    volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == storage.volume_id).first()
    if not volume: raise HTTPException(status_code=404, detail="Storage volume not found")
    if storage.quota_gb <= 0: raise HTTPException(status_code=400, detail="quota_gb must be positive")
    if volume.used_capacity_gb + storage.quota_gb > volume.total_capacity_gb: raise HTTPException(status_code=409, detail="Storage volume capacity exceeded")
    current = get_project_storage_usage_gb(db, project.id)
    if project.max_storage_gb > 0 and current + storage.quota_gb > project.max_storage_gb: raise HTTPException(status_code=409, detail="Project storage quota exceeded")
    if db.query(models.UserStorage).filter(models.UserStorage.user_id == storage.user_id, models.UserStorage.volume_id == storage.volume_id).first(): raise HTTPException(status_code=400, detail="User already has storage on this volume")
    data = storage.model_dump(); data["project_id"] = project.id
    data["folder_path"] = data.get("folder_path") or f"project_{project.id}/user_{user.id}/storage_auto"
    row = models.UserStorage(**data)
    try: db.add(row); volume.used_capacity_gb += storage.quota_gb; db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="User storage creation failed")

@router.delete("/api/user-storages/{storage_id}")
def delete_user_storage(storage_id: int, db: Session = Depends(get_db)):
    row = db.query(models.UserStorage).filter(models.UserStorage.id == storage_id).first()
    if not row: raise HTTPException(status_code=404, detail="User storage not found")
    try:
        volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == row.volume_id).first()
        if volume: volume.used_capacity_gb = max((volume.used_capacity_gb or 0) - (row.quota_gb or 0), 0)
        db.delete(row); db.commit(); return {"message":"User storage deleted", "id":storage_id}
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="User storage deletion failed")

@router.put("/api/user-storages/{storage_id}/quota")
def update_user_storage_quota(storage_id: int, payload: dict, db: Session = Depends(get_db)):
    row = db.query(models.UserStorage).filter(models.UserStorage.id == storage_id).first()
    if not row: raise HTTPException(status_code=404, detail="User storage not found")
    new_quota = payload.get("quota_gb")
    if new_quota is None: raise HTTPException(status_code=400, detail="quota_gb is required")
    new_quota = int(new_quota)
    if new_quota <= 0: raise HTTPException(status_code=400, detail="quota_gb must be positive")
    try:
        volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == row.volume_id).first(); delta = new_quota - row.quota_gb
        if volume and volume.used_capacity_gb + delta > volume.total_capacity_gb: raise HTTPException(status_code=409, detail="Storage volume capacity exceeded")
        project = validate_project_exists(db, row.project_id)
        current = get_project_storage_usage_gb(db, row.project_id)
        if project.max_storage_gb > 0 and current + delta > project.max_storage_gb: raise HTTPException(status_code=409, detail="Project storage quota exceeded")
        if volume: volume.used_capacity_gb += delta
        row.quota_gb = new_quota; db.commit(); db.refresh(row); return {"message":"Quota updated", "id":storage_id, "new_quota_gb":new_quota}
    except HTTPException: raise
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Quota update failed")
