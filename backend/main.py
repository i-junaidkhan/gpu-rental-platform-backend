import logging
import uuid
import os
import json
import ssl
import urllib.request
import urllib.parse
import urllib.error

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from kubernetes import client, config

from database import get_db
import models
from schemas import (
    UserCreate,
    UserUpdate,
    UserResponse,
    ProjectCreate,
    ProjectUpdate,
    ProjectResponse,
    PlanCreate,
    PlanUpdate,
    PlanResponse,
    InstanceCreate,
    InstanceResponse,
    StorageVolumeCreate,
    StorageVolumeUpdate,
    StorageVolumeResponse,
    UserStorageCreate,
    UserStorageResponse,
    InstancePortCreate,
    InstancePortResponse,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =========================
# Raw Kubernetes API helper
# =========================
# The installed kubernetes Python client authenticates as system:anonymous in this cluster.
# Direct urllib requests with the same ServiceAccount token are proven to work, so all
# dashboard/discovery read endpoints use this raw helper.

def _k8s_raw_base():
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    return f"https://{host}:{port}"


def _k8s_raw_token():
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    with open(token_path) as f:
        return f.read().strip()


def _k8s_raw_ssl_context():
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(ca_path)
    return ctx


def k8s_raw_request(method: str, path: str, body: dict | None = None) -> dict:
    url = _k8s_raw_base() + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Authorization", f"Bearer {_k8s_raw_token()}")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=_k8s_raw_ssl_context(), timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Kubernetes raw API error {e.code} for {path}: {error_body}")
        raise HTTPException(status_code=e.code, detail=f"Kubernetes error: {error_body}")
    except Exception as e:
        logger.error(f"Kubernetes raw API request failed for {path}: {e}")
        raise HTTPException(status_code=500, detail=f"Kubernetes raw API request failed: {e}")


def k8s_raw_list_nodes() -> list[dict]:
    return k8s_raw_request("GET", "/api/v1/nodes").get("items", [])


def k8s_raw_read_node(name: str) -> dict:
    return k8s_raw_request("GET", f"/api/v1/nodes/{urllib.parse.quote(name)}")


def k8s_raw_list_namespaces() -> list[dict]:
    return k8s_raw_request("GET", "/api/v1/namespaces").get("items", [])


def k8s_raw_list_pods(namespace: str | None = None, field_selector: str | None = None) -> list[dict]:
    if namespace:
        path = f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods"
    else:
        path = "/api/v1/pods"
    if field_selector:
        path += "?fieldSelector=" + urllib.parse.quote(field_selector)
    return k8s_raw_request("GET", path).get("items", [])


def k8s_raw_list_services(namespace: str | None = None) -> list[dict]:
    if namespace:
        path = f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/services"
    else:
        path = "/api/v1/services"
    return k8s_raw_request("GET", path).get("items", [])


def _raw_ts(meta: dict) -> str | None:
    return (meta or {}).get("creationTimestamp")


def _raw_node_conditions(node: dict) -> dict:
    return {c.get("type"): c.get("status") for c in node.get("status", {}).get("conditions", [])}


def raw_node_to_dict(node: dict) -> dict:
    meta = node.get("metadata", {})
    status = node.get("status", {})
    labels = meta.get("labels", {}) or {}
    capacity = status.get("capacity", {}) or {}
    allocatable = status.get("allocatable", {}) or {}
    conditions = _raw_node_conditions(node)
    return {
        "name": meta.get("name"),
        "status": "Ready" if conditions.get("Ready") == "True" else "NotReady",
        "ready": conditions.get("Ready") == "True",
        "roles": labels,
        "cpu": allocatable.get("cpu") or capacity.get("cpu"),
        "memory": allocatable.get("memory") or capacity.get("memory"),
        "gpu": allocatable.get("nvidia.com/gpu", capacity.get("nvidia.com/gpu", "0")),
        "mig_1g_10gb": allocatable.get("nvidia.com/mig-1g.10gb", capacity.get("nvidia.com/mig-1g.10gb", "0")),
        "capacity": capacity,
        "allocatable": allocatable,
        "labels": labels,
        "internal_ip": next((a.get("address") for a in status.get("addresses", []) if a.get("type") == "InternalIP"), None),
        "created_at": _raw_ts(meta),
    }


def raw_pod_to_dict(pod: dict) -> dict:
    meta = pod.get("metadata", {})
    status = pod.get("status", {})
    spec = pod.get("spec", {})
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "status": status.get("phase"),
        "node": spec.get("nodeName"),
        "pod_ip": status.get("podIP"),
        "host_ip": status.get("hostIP"),
        "labels": meta.get("labels", {}) or {},
        "created_at": _raw_ts(meta),
    }


def raw_service_to_dict(svc: dict) -> dict:
    meta = svc.get("metadata", {})
    spec = svc.get("spec", {})
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "type": spec.get("type"),
        "cluster_ip": spec.get("clusterIP"),
        "ports": [
            {
                "name": p.get("name"),
                "port": p.get("port"),
                "target_port": str(p.get("targetPort")),
                "node_port": p.get("nodePort"),
                "protocol": p.get("protocol"),
            }
            for p in spec.get("ports", [])
        ],
    }

# Kubernetes client setup with manual token configuration (fixes kubernetes-client v36.0.0 bug)
# Create a GLOBAL k8s_v1 client that all endpoints will use
k8s_v1 = None

try:
    token_path = '/var/run/secrets/kubernetes.io/serviceaccount/token'
    if os.path.exists(token_path):
        with open(token_path) as f:
            token = f.read().strip()
        configuration = client.Configuration()
        configuration.api_key['authorization'] = token
        configuration.api_key_prefix['authorization'] = 'Bearer'
        k8s_host = os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')
        k8s_port = os.environ.get('KUBERNETES_SERVICE_PORT', '443')
        configuration.host = f"https://{k8s_host}:{k8s_port}"
        configuration.ssl_ca_cert = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
        api_client = client.ApiClient(configuration)
        k8s_v1 = client.CoreV1Api(api_client)
        logger.info("Loaded in-cluster Kubernetes config (manual token).")
    else:
        config.load_kube_config()
        k8s_v1 = client.CoreV1Api()
        logger.info("Loaded local kube config.")
except Exception as e:
    logger.warning(f"Could not load Kubernetes config: {e}")

app = FastAPI(title="GPU Rental API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        db_status = "error"

    return {
        "status": "healthy",
        "database": db_status,
    }


@app.get("/api/tables")
def list_tables(db: Session = Depends(get_db)):
    result = db.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        )
    )
    return {"tables": [row[0] for row in result.fetchall()]}




# =========================
# Projects
# =========================

@app.get("/api/projects", response_model=list[ProjectResponse])
def get_projects(db: Session = Depends(get_db)):
    return db.query(models.Project).order_by(models.Project.id).all()


@app.post("/api/projects", response_model=ProjectResponse)
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    db_project = models.Project(**project.model_dump())
    try:
        db.add(db_project)
        db.commit()
        db.refresh(db_project)
        return db_project
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Project creation failed: {e}")
        raise HTTPException(status_code=400, detail="Project creation failed")


@app.get("/api/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.put("/api/projects/{project_id}", response_model=ProjectResponse)
def update_project(project_id: int, payload: ProjectUpdate, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(project, key, value)

    try:
        db.commit()
        db.refresh(project)
        return project
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Project update failed: {e}")
        raise HTTPException(status_code=400, detail="Project update failed")


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if db.query(models.User).filter(models.User.project_id == project_id).first():
        raise HTTPException(status_code=400, detail="Cannot delete project while users are assigned")

    try:
        db.delete(project)
        db.commit()
        return {"message": "Project deleted", "id": project_id}
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Project deletion failed: {e}")
        raise HTTPException(status_code=400, detail="Project deletion failed")


@app.get("/api/projects/{project_id}/summary")
def get_project_summary(project_id: int, db: Session = Depends(get_db)):
    project = validate_project_exists(db, project_id)

    users_count = db.query(models.User).filter(models.User.project_id == project_id).count()
    active_instances = db.query(models.Instance).filter(
        models.Instance.project_id == project_id,
        models.Instance.status.in_([models.InstanceStatusEnum.RUNNING, models.InstanceStatusEnum.PENDING])
    ).all()

    gpu_usage = 0
    for inst in active_instances:
        plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == inst.plan_id).first()
        if plan:
            gpu_usage += int(plan.resource_count or 0)

    storage_allocated_gb = get_project_storage_usage_gb(db, project_id)

    return {
        "project_id": project.id,
        "project_name": project.name,
        "max_gpu_count": project.max_gpu_count,
        "gpu_used": gpu_usage,
        "gpu_available": None if (project.id == 1 and project.max_gpu_count == 0) else max(project.max_gpu_count - gpu_usage, 0),
        "max_storage_gb": project.max_storage_gb,
        "storage_allocated_gb": storage_allocated_gb,
        "storage_available_gb": None if project.max_storage_gb == 0 else max(project.max_storage_gb - storage_allocated_gb, 0),
        "users_count": users_count,
        "active_instances_count": len(active_instances),
    }


# =========================
# Users
# =========================

@app.post("/api/users", response_model=UserResponse)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    data = user.model_dump()
    project_id = data.get("project_id")
    if project_id is not None:
        validate_project_exists(db, project_id)
    db_user = models.User(**data)

    try:
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        return db_user
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"User creation failed: {e}")
        raise HTTPException(status_code=400, detail="User creation failed")


@app.get("/api/users", response_model=list[UserResponse])
def get_users(project_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(models.User)
    if project_id is not None:
        query = query.filter(models.User.project_id == project_id)
    return query.order_by(models.User.id).all()


@app.get("/api/users/{user_id}", response_model=UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.put("/api/users/{user_id}", response_model=UserResponse)
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    updates = payload.model_dump(exclude_unset=True)
    if "project_id" in updates and updates["project_id"] is not None:
        validate_project_exists(db, updates["project_id"])

    for key, value in updates.items():
        setattr(user, key, value)

    try:
        db.commit()
        db.refresh(user)
        return user
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"User update failed: {e}")
        raise HTTPException(status_code=400, detail="User update failed")


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if db.query(models.Instance).filter(
        models.Instance.user_id == user_id,
        models.Instance.status.in_([models.InstanceStatusEnum.RUNNING, models.InstanceStatusEnum.PENDING])
    ).first():
        raise HTTPException(status_code=400, detail="Cannot delete user with running/pending instances")
    try:
        db.delete(user)
        db.commit()
        return {"message": "User deleted", "id": user_id}
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"User deletion failed: {e}")
        raise HTTPException(status_code=400, detail="User deletion failed")

# =========================
# Rental Plans
# =========================

@app.post("/api/rental-plans", response_model=PlanResponse)
def create_plan(plan: PlanCreate, db: Session = Depends(get_db)):
    db_plan = models.RentalPlan(**plan.model_dump())

    try:
        db.add(db_plan)
        db.commit()
        db.refresh(db_plan)
        return db_plan
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Plan creation failed: {e}")
        raise HTTPException(status_code=400, detail="Plan creation failed")


@app.get("/api/rental-plans", response_model=list[PlanResponse])
def get_plans(db: Session = Depends(get_db)):
    return db.query(models.RentalPlan).order_by(models.RentalPlan.id).all()


@app.put("/api/rental-plans/{plan_id}", response_model=PlanResponse)
def update_plan(plan_id: int, payload: PlanUpdate, db: Session = Depends(get_db)):
    plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(plan, key, value)
    try:
        db.commit()
        db.refresh(plan)
        return plan
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Plan update failed: {e}")
        raise HTTPException(status_code=400, detail="Plan update failed")


@app.delete("/api/rental-plans/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if db.query(models.Instance).filter(models.Instance.plan_id == plan_id).first():
        raise HTTPException(status_code=400, detail="Cannot delete plan with existing instances")
    try:
        db.delete(plan)
        db.commit()
        return {"message": "Plan deleted", "id": plan_id}
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Plan deletion failed: {e}")
        raise HTTPException(status_code=400, detail="Plan deletion failed")


# =========================
# Instances
# =========================

@app.get("/api/instances", response_model=list[InstanceResponse])
def get_instances(db: Session = Depends(get_db)):
    return db.query(models.Instance).order_by(models.Instance.id).all()


def normalize_image_name(image: str) -> str:
    if image.startswith("docker.io/") or image.startswith("nvcr.io/") or image.startswith("quay.io/") or image.startswith("registry.k8s.io/"):
        return image
    if "/" not in image:
        return f"docker.io/library/{image}"
    return f"docker.io/{image}"



ALLOWED_INSTANCE_IMAGES = {
    "ubuntu:22.04",
    "docker.io/library/ubuntu:22.04",
    "nvidia/cuda:12.0-base-ubuntu22.04",
    "docker.io/nvidia/cuda:12.0-base-ubuntu22.04",
    "nvidia/cuda:11.8-base-ubuntu22.04",
    "docker.io/nvidia/cuda:11.8-base-ubuntu22.04",
    "pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime",
    "docker.io/pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime",
    "tensorflow/tensorflow:2.13.0-gpu",
    "docker.io/tensorflow/tensorflow:2.13.0-gpu",
    "jupyter/tensorflow-notebook:latest",
    "docker.io/jupyter/tensorflow-notebook:latest",
}

def normalize_and_validate_image(image: str) -> str:
    safe_image = normalize_image_name(image)
    if image not in ALLOWED_INSTANCE_IMAGES and safe_image not in ALLOWED_INSTANCE_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Image is not allowed: {image}. Add it to the backend allowlist first."
        )
    return safe_image

def get_project_storage_usage_gb(db: Session, project_id: int) -> int:
    rows = db.query(models.UserStorage).filter(models.UserStorage.project_id == project_id).all()
    return int(sum(int(row.quota_gb or 0) for row in rows))

def validate_project_exists(db: Session, project_id: int):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project

def get_instance_command(app_type: str | None):
    # Keep runtime safe: all modes start a long-running container for now.
    # Real Jupyter/VSCode launch needs image-specific commands and service exposure.
    return ["sleep", "infinity"]



def app_default_port(app_type: str | None) -> int:
    app = (app_type or "terminal").lower()
    if app == "jupyter":
        return 8888
    if app == "vscode":
        return 8080
    if app == "ssh":
        return 22
    return 8888


def build_launch_url(node_ip: str, node_port: int, app_type: str | None = None) -> str:
    scheme = "ssh" if (app_type or "").lower() == "ssh" else "http"
    return f"{scheme}://{node_ip}:{node_port}"


def get_node_external_ip(node_name: str = "g01") -> str:
    try:
        node = k8s_raw_read_node(node_name)
        for addr in node.get("status", {}).get("addresses", []) or []:
            if addr.get("type") in ["ExternalIP", "InternalIP"]:
                return addr.get("address")
    except Exception as e:
        logger.warning(f"Failed to read node IP through raw Kubernetes API: {e}")
    return "192.168.10.226"


def calculate_resource_usage(node_name: str, resource_name: str) -> dict:
    node = k8s_raw_read_node(node_name)
    allocatable = node.get("status", {}).get("allocatable", {}) or {}
    try:
        capacity = int(allocatable.get(resource_name, "0"))
    except Exception:
        capacity = 0
    pods = k8s_raw_list_pods(field_selector=f"spec.nodeName={node_name}")
    used = 0
    for pod in pods:
        if pod.get("status", {}).get("phase") in ["Succeeded", "Failed"]:
            continue
        for container in pod.get("spec", {}).get("containers", []) or []:
            limits = container.get("resources", {}).get("limits", {}) or {}
            if resource_name in limits:
                try:
                    used += int(limits.get(resource_name, 0))
                except Exception:
                    pass
    return {"capacity": capacity, "used": used, "free": max(capacity - used, 0)}



@app.post("/api/instances", response_model=InstanceResponse)
def create_instance(instance: InstanceCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == instance.user_id).first()
    plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == instance.plan_id).first()
    project = db.query(models.Project).filter(models.Project.id == instance.project_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # PHASE 2A: GPU Quota Enforcement
    requested_count = int(plan.resource_count)
    
    current_instances = db.query(models.Instance).filter(
        models.Instance.project_id == project.id,
        models.Instance.status.in_([models.InstanceStatusEnum.RUNNING, models.InstanceStatusEnum.PENDING])
    ).all()

    current_gpu_usage = 0
    for inst in current_instances:
        inst_plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == inst.plan_id).first()
        if inst_plan:
            current_gpu_usage += inst_plan.resource_count

    # Quota Logic: 0 means unlimited ONLY for default-project (id 1)
    if project.id == 1 and project.max_gpu_count == 0:
        pass  # Unlimited bypass for default project
    elif (current_gpu_usage + requested_count) > project.max_gpu_count:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Project '{project.name}' GPU quota exceeded.",
                "max_allowed": project.max_gpu_count,
                "currently_using": current_gpu_usage,
                "requested": requested_count
            }
        )

    # --- YOUR EXISTING K8s LOGIC REMAINS UNTOUCHED ---
    namespace = "gpu-rental-system"
    node_name = "g01"
    resource_name = plan.k8s_resource_name
    safe_image = normalize_and_validate_image(instance.image)

    try:
        v1 = k8s_v1
        usage = calculate_resource_usage(node_name, resource_name)

        if usage["free"] < requested_count:
            raise HTTPException(
                status_code=409,
                detail=f"No available Kubernetes capacity for {resource_name}"
            )

        short_uuid = uuid.uuid4().hex[:6]
        pod_name = f"gpu-p{project.id}-u{user.id}-{short_uuid}"

        limits = {resource_name: str(requested_count)}
        requests = {}

        if instance.cpu_cores is not None:
            cpu_value = str(instance.cpu_cores)
            limits["cpu"] = cpu_value
            requests["cpu"] = cpu_value

        if instance.memory_gb is not None:
            mem_value = f"{instance.memory_gb}Gi"
            limits["memory"] = mem_value
            requests["memory"] = mem_value

        volumes = []
        volume_mounts = []
        pvc_name = "mock-pvc-for-now"

        if instance.storage_id is not None:
            storage = db.query(models.UserStorage).filter(models.UserStorage.id == instance.storage_id).first()
            if not storage:
                raise HTTPException(status_code=404, detail="Storage allocation not found")
            if storage.user_id != user.id or storage.project_id != project.id:
                raise HTTPException(status_code=403, detail="Storage allocation does not belong to this user/project")
            pvc_name = f"user-storage-{storage.id}"
            volumes.append(
                client.V1Volume(
                    name="workspace-storage",
                    host_path=client.V1HostPathVolumeSource(
                        path=storage.folder_path,
                        type="DirectoryOrCreate"
                    )
                )
            )
            volume_mounts.append(
                client.V1VolumeMount(
                    name="workspace-storage",
                    mount_path="/workspace"
                )
            )

        if instance.shm_gb is not None and instance.shm_gb > 0:
            volumes.append(
                client.V1Volume(
                    name="dshm",
                    empty_dir=client.V1EmptyDirVolumeSource(
                        medium="Memory",
                        size_limit=f"{instance.shm_gb}Gi"
                    )
                )
            )
            volume_mounts.append(
                client.V1VolumeMount(
                    name="dshm",
                    mount_path="/dev/shm"
                )
            )

        pod_manifest = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                labels={
                    "app": "gpu-tenant-instance",
                    "user_id": str(user.id),
                    "project_id": str(project.id),
                    "plan_id": str(plan.id),
                    "billing": "true",
                    "app_type": str(instance.app_type or "terminal"),
                },
            ),
            spec=client.V1PodSpec(
                node_name=node_name,
                restart_policy="Never",
                volumes=volumes or None,
                containers=[
                    client.V1Container(
                        name="ai-workspace",
                        image=safe_image,
                        command=get_instance_command(instance.app_type),
                        volume_mounts=volume_mounts or None,
                        resources=client.V1ResourceRequirements(
                            limits=limits,
                            requests=requests or None,
                        ),
                    )
                ],
            ),
        )

        v1.create_namespaced_pod(namespace=namespace, body=pod_manifest)
        logger.info(f"Created pod {pod_name} using {resource_name}:{requested_count} for project {project.id}")

    except HTTPException:
        raise
    except client.exceptions.ApiException as e:
        logger.error(f"Kubernetes pod creation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Kubernetes pod creation failed: {e.reason}")
    except Exception as e:
        logger.error(f"Unexpected instance creation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # --- SAVE TO DB (NOW WITH PROJECT ID) ---
    db_instance = models.Instance(
        user_id=user.id,
        project_id=project.id,  # Added project binding
        plan_id=plan.id,
        pod_name=pod_name,
        namespace=namespace,
        pvc_name=pvc_name,
        status=models.InstanceStatusEnum.RUNNING,
    )

    try:
        db.add(db_instance)
        db.commit()
        db.refresh(db_instance)
        return db_instance
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Database error saving instance: {e}")
        try:
            v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Pod created but DB save failed; cleanup attempted")


@app.delete("/api/instances/{instance_id}")
def delete_instance(instance_id: int, db: Session = Depends(get_db)):
    instance = db.query(models.Instance).filter(models.Instance.id == instance_id).first()

    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    try:
        v1 = k8s_v1
        v1.delete_namespaced_pod(name=instance.pod_name, namespace=instance.namespace)
        logger.info(f"Deleted pod {instance.pod_name}")
    except client.exceptions.ApiException as e:
        if e.status != 404:
            logger.error(f"Failed to delete pod {instance.pod_name}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to delete pod: {e.reason}")

    instance.status = models.InstanceStatusEnum.DELETED
    db.commit()

    return {"message": "Instance deleted", "id": instance.id, "pod_name": instance.pod_name, "status": instance.status.value}


@app.post("/api/instances/{instance_id}/action")
def instance_action(instance_id: int, payload: dict, db: Session = Depends(get_db)):
    instance = db.query(models.Instance).filter(models.Instance.id == instance_id).first()

    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    action = payload.get("action", "").lower().strip()

    if action not in ["start", "stop", "restart"]:
        raise HTTPException(status_code=400, detail="Invalid action")

    v1 = k8s_v1

    if action in ["stop", "restart"]:
        try:
            v1.delete_namespaced_pod(name=instance.pod_name, namespace=instance.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise HTTPException(status_code=500, detail=f"Failed to stop pod: {e.reason}")

        instance.status = models.InstanceStatusEnum.STOPPED
        db.commit()

        if action == "stop":
            return {"message": "Instance stopped", "id": instance.id, "pod_name": instance.pod_name, "status": instance.status.value}

    if action in ["start", "restart"]:
        plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == instance.plan_id).first()

        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

        node_name = "g01"
        resource_name = plan.k8s_resource_name
        requested_count = int(plan.resource_count)

        usage = calculate_resource_usage(node_name, resource_name)

        if usage["free"] < requested_count:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"No available capacity for {resource_name}",
                    "resource": resource_name,
                    "capacity": usage["capacity"],
                    "used": usage["used"],
                    "free": usage["free"],
                    "requested": requested_count,
                },
            )

        image = "docker.io/nvidia/cuda:12.4.0-base-ubuntu22.04"

        pod_manifest = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=instance.pod_name,
                labels={
                    "app": "gpu-tenant-instance",
                    "user_id": str(instance.user_id),
                    "plan_id": str(instance.plan_id),
                    "billing": "true",
                },
            ),
            spec=client.V1PodSpec(
                node_name=node_name,
                restart_policy="Never",
                containers=[
                    client.V1Container(
                        name="ai-workspace",
                        image=image,
                        command=["sleep", "infinity"],
                        resources=client.V1ResourceRequirements(
                            limits={resource_name: str(requested_count)}
                        ),
                    )
                ],
            ),
        )

        try:
            v1.create_namespaced_pod(namespace=instance.namespace, body=pod_manifest)
        except client.exceptions.ApiException as e:
            raise HTTPException(status_code=500, detail=f"Failed to start pod: {e.reason}")

        instance.status = models.InstanceStatusEnum.RUNNING
        db.commit()

        return {"message": "Instance started", "id": instance.id, "pod_name": instance.pod_name, "status": instance.status.value}



@app.get("/api/instances/{instance_id}/ports", response_model=list[InstancePortResponse])
def list_instance_ports(instance_id: int, db: Session = Depends(get_db)):
    instance = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    return db.query(models.InstancePort).filter(models.InstancePort.instance_id == instance_id).all()


@app.post("/api/instances/{instance_id}/ports", response_model=InstancePortResponse)
def open_instance_port(instance_id: int, payload: InstancePortCreate, db: Session = Depends(get_db)):
    instance = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    target_port = int(payload.target_port or payload.port)
    exposed_port = int(payload.port)
    protocol = (payload.protocol or "TCP").upper()
    service_name = f"{instance.pod_name}-port-{exposed_port}".lower().replace("_", "-")[:63]

    try:
        v1 = k8s_v1
        service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=service_name,
                namespace=instance.namespace,
                labels={
                    "app": "gpu-tenant-instance-port",
                    "instance_id": str(instance.id),
                    "pod_name": instance.pod_name,
                },
            ),
            spec=client.V1ServiceSpec(
                type="NodePort",
                selector={"app": "gpu-tenant-instance", "pod_name": instance.pod_name},
                ports=[
                    client.V1ServicePort(
                        name=f"port-{exposed_port}",
                        port=exposed_port,
                        target_port=target_port,
                        protocol=protocol,
                    )
                ],
            ),
        )

        try:
            created = v1.create_namespaced_service(namespace=instance.namespace, body=service)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                created = v1.read_namespaced_service(name=service_name, namespace=instance.namespace)
            else:
                raise

        node_port = None
        if created.spec and created.spec.ports:
            node_port = created.spec.ports[0].node_port

        node_ip = get_node_external_ip("g01")
        launch_url = build_launch_url(node_ip, node_port, None) if node_port else None

    except client.exceptions.ApiException as e:
        logger.error(f"Failed to open service port for instance {instance_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Kubernetes service creation failed: {e.reason}")
    except Exception as e:
        logger.error(f"Unexpected port open error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    existing = db.query(models.InstancePort).filter(
        models.InstancePort.instance_id == instance_id,
        models.InstancePort.port == exposed_port,
        models.InstancePort.status == "open",
    ).first()

    if existing:
        existing.target_port = target_port
        existing.node_port = node_port
        existing.protocol = protocol
        existing.service_name = service_name
        existing.launch_url = launch_url
        db.commit()
        db.refresh(existing)
        return existing

    row = models.InstancePort(
        instance_id=instance_id,
        port=exposed_port,
        target_port=target_port,
        node_port=node_port,
        protocol=protocol,
        service_name=service_name,
        launch_url=launch_url,
        status="open",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.delete("/api/instances/{instance_id}/ports/{port_id}")
def close_instance_port(instance_id: int, port_id: int, db: Session = Depends(get_db)):
    instance = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    port_row = db.query(models.InstancePort).filter(
        models.InstancePort.id == port_id,
        models.InstancePort.instance_id == instance_id,
    ).first()
    if not port_row:
        raise HTTPException(status_code=404, detail="Port record not found")

    try:
        v1 = k8s_v1
        v1.delete_namespaced_service(name=port_row.service_name, namespace=instance.namespace)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=f"Failed to delete service: {e.reason}")

    port_row.status = "closed"
    db.commit()
    return {"message": "Port closed", "id": port_row.id, "port": port_row.port}


@app.get("/api/instances/{instance_id}/launch")
def get_instance_launch(instance_id: int, db: Session = Depends(get_db)):
    instance = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    open_ports = db.query(models.InstancePort).filter(
        models.InstancePort.instance_id == instance_id,
        models.InstancePort.status == "open",
    ).all()
    return {
        "instance_id": instance.id,
        "pod_name": instance.pod_name,
        "status": instance.status.value if hasattr(instance.status, "value") else str(instance.status),
        "ports": [
            {
                "id": p.id,
                "port": p.port,
                "target_port": p.target_port,
                "node_port": p.node_port,
                "launch_url": p.launch_url,
                "status": p.status,
            }
            for p in open_ports
        ],
    }


@app.get("/api/monitoring/pods")
def monitoring_pods(db: Session = Depends(get_db)):
    instances = db.query(models.Instance).all()
    return [
        {
            "instance_id": i.id,
            "pod_name": i.pod_name,
            "namespace": i.namespace,
            "project_id": i.project_id,
            "user_id": i.user_id,
            "plan_id": i.plan_id,
            "status": i.status.value if hasattr(i.status, "value") else str(i.status),
            "accumulated_cost": i.accumulated_cost or 0.0,
        }
        for i in instances
    ]


@app.get("/api/monitoring/nodes")
def monitoring_nodes():
    nodes = k8s_raw_list_nodes()
    result = []
    for node in nodes:
        nd = raw_node_to_dict(node)
        result.append({
            "name": nd.get("name"),
            "ready": nd.get("ready"),
            "cpu": nd.get("capacity", {}).get("cpu"),
            "memory": nd.get("capacity", {}).get("memory"),
            "gpu": nd.get("capacity", {}).get("nvidia.com/gpu", "0"),
            "internal_ip": nd.get("internal_ip"),
            "status": nd.get("status"),
            "labels": nd.get("labels", {}),
        })
    return result

@app.get("/api/monitoring/gpus")
def monitoring_gpus():
    nodes = k8s_raw_list_nodes()
    rows = []
    for node in nodes:
        meta = node.get("metadata", {})
        status = node.get("status", {})
        labels = meta.get("labels", {}) or {}
        capacity = status.get("capacity", {}) or {}
        allocatable = status.get("allocatable", {}) or {}
        found = False
        for key in sorted(set(list(capacity.keys()) + list(allocatable.keys()))):
            if key.startswith("nvidia.com/"):
                found = True
                try:
                    cap = int(capacity.get(key, "0"))
                except Exception:
                    cap = 0
                try:
                    alloc = int(allocatable.get(key, "0"))
                except Exception:
                    alloc = 0
                rows.append({
                    "node_name": meta.get("name"),
                    "resource_name": key,
                    "capacity": cap,
                    "allocatable": alloc,
                    "product": labels.get("nvidia.com/gpu.product") or labels.get(f"{key}.product"),
                    "mig_strategy": labels.get("nvidia.com/mig.strategy"),
                    "status": "available" if alloc > 0 else "gpu_present_not_advertised",
                })
        if not found and (labels.get("feature.node.kubernetes.io/pci-10de.present") == "true" or labels.get("nvidia.com/gpu.present") == "true"):
            rows.append({
                "node_name": meta.get("name"),
                "resource_name": "nvidia.com/gpu",
                "capacity": 0,
                "allocatable": 0,
                "product": labels.get("nvidia.com/gpu.product"),
                "mig_strategy": labels.get("nvidia.com/mig.strategy"),
                "status": "gpu_present_not_advertised",
            })
    return rows

@app.get("/api/billing/usage/raw")
def billing_usage_raw(db: Session = Depends(get_db)):
    instances = db.query(models.Instance).all()
    rows = []
    for i in instances:
        plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == i.plan_id).first()
        rate = float(plan.price_per_hour) if plan else 0.0
        rows.append({
            "instance_id": i.id,
            "pod_name": i.pod_name,
            "user_id": i.user_id,
            "project_id": i.project_id,
            "plan_id": i.plan_id,
            "status": i.status.value if hasattr(i.status, "value") else str(i.status),
            "price_per_hour": rate,
            "accumulated_cost": float(i.accumulated_cost or 0.0),
        })
    return rows


@app.get("/api/billing/usage/summary")
def billing_usage_summary(period: str = "daily", db: Session = Depends(get_db)):
    rows = billing_usage_raw(db)
    total_cost = sum(float(r.get("accumulated_cost") or 0.0) for r in rows)
    active_count = sum(1 for r in rows if str(r.get("status")).lower() == "running")
    return {
        "period": period,
        "total_instances": len(rows),
        "active_instances": active_count,
        "total_accumulated_cost": total_cost,
        "by_project": [
            {
                "project_id": pid,
                "instances": len(items),
                "active_instances": sum(1 for r in items if str(r.get("status")).lower() == "running"),
                "accumulated_cost": sum(float(r.get("accumulated_cost") or 0.0) for r in items),
            }
            for pid, items in _group_by_project(rows).items()
        ],
    }


def _group_by_project(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(row.get("project_id"), []).append(row)
    return grouped


# =========================
# Kubernetes Discovery
# =========================

@app.get("/api/k8s/node-resources/{node_name}")
def get_node_resources(node_name: str):
    try:
        node = k8s_raw_read_node(node_name)
        status = node.get("status", {})
        capacity = status.get("capacity", {}) or {}
        allocatable = status.get("allocatable", {}) or {}
        labels = node.get("metadata", {}).get("labels", {}) or {}
        gpu_resources = {}
        for key in sorted(set(list(capacity.keys()) + list(allocatable.keys()))):
            if key.startswith("nvidia.com/"):
                gpu_resources[key] = {"capacity": capacity.get(key, "0"), "allocatable": allocatable.get(key, "0")}
        return {
            "node": node_name,
            "gpu_resources": gpu_resources,
            "labels": {
                "gpu_count": labels.get("nvidia.com/gpu.count"),
                "gpu_product": labels.get("nvidia.com/gpu.product"),
                "gpu_memory": labels.get("nvidia.com/gpu.memory"),
                "gpu_replicas": labels.get("nvidia.com/gpu.replicas"),
                "gpu_sharing_strategy": labels.get("nvidia.com/gpu.sharing-strategy"),
                "mig_capable": labels.get("nvidia.com/mig.capable"),
                "mig_strategy": labels.get("nvidia.com/mig.strategy"),
                "mig_config": labels.get("nvidia.com/mig.config"),
                "mig_config_state": labels.get("nvidia.com/mig.config.state"),
                "mig_1g_10gb_count": labels.get("nvidia.com/mig-1g.10gb.count"),
                "mig_1g_10gb_product": labels.get("nvidia.com/mig-1g.10gb.product"),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch node resources: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/pods/{namespace}/{pod_name}/logs")
def get_pod_logs(namespace: str, pod_name: str, tail_lines: int = 100):
    try:
        logs = k8s_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=tail_lines)
        return {"logs": logs}
    except client.exceptions.ApiException as e:
        if e.status in [400, 404]:
            return {"logs": "", "message": f"Logs unavailable: {e.reason}"}
        logger.error(f"Failed to fetch logs for {pod_name}: {e}")
        raise HTTPException(status_code=e.status, detail=f"Kubernetes error: {e.reason}")


# =========================
# Frontend Compatibility Endpoints
# =========================

def pod_to_dict(pod):
    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "status": pod.status.phase,
        "node": pod.spec.node_name,
        "pod_ip": pod.status.pod_ip,
        "host_ip": pod.status.host_ip,
        "labels": pod.metadata.labels or {},
        "created_at": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
    }


def node_to_dict(node):
    capacity = node.status.capacity or {}
    allocatable = node.status.allocatable or {}
    labels = node.metadata.labels or {}
    return {
        "name": node.metadata.name,
        "status": "Ready",
        "roles": labels,
        "cpu": allocatable.get("cpu"),
        "memory": allocatable.get("memory"),
        "gpu": allocatable.get("nvidia.com/gpu", "0"),
        "mig_1g_10gb": allocatable.get("nvidia.com/mig-1g.10gb", "0"),
        "capacity": capacity,
        "allocatable": allocatable,
        "labels": labels,
    }


@app.get("/api/nodes")
def get_nodes():
    return [raw_node_to_dict(n) for n in k8s_raw_list_nodes()]

@app.get("/api/namespaces")
def get_namespaces():
    return [
        {"name": ns.get("metadata", {}).get("name"), "status": ns.get("status", {}).get("phase"), "created_at": _raw_ts(ns.get("metadata", {}))}
        for ns in k8s_raw_list_namespaces()
    ]

@app.get("/api/pods")
def get_pods(namespace: str = None):
    return [raw_pod_to_dict(p) for p in k8s_raw_list_pods(namespace=namespace)]

@app.get("/api/services")
def get_services(namespace: str = None):
    return [raw_service_to_dict(s) for s in k8s_raw_list_services(namespace=namespace)]

@app.delete("/api/pods/{namespace}/{pod_name}")
def delete_pod(namespace: str, pod_name: str):
    try:
        k8s_v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        return {"message": "Pod deleted", "namespace": namespace, "pod_name": pod_name}
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return {"message": "Pod already missing", "namespace": namespace, "pod_name": pod_name}
        raise HTTPException(status_code=e.status, detail=f"Kubernetes error: {e.reason}")


@app.get("/api/gpu-inventory")
def get_gpu_inventory():
    try:
        node = k8s_raw_read_node("g01")
        status = node.get("status", {})
        allocatable = status.get("allocatable", {}) or {}
        capacity = status.get("capacity", {}) or {}
        pods = k8s_raw_list_pods(namespace="gpu-rental-system")
        used = {"nvidia.com/gpu": 0, "nvidia.com/mig-1g.10gb": 0}
        for pod in pods:
            for container in pod.get("spec", {}).get("containers", []) or []:
                limits = container.get("resources", {}).get("limits", {}) or {}
                for key in used:
                    if key in limits:
                        try:
                            used[key] += int(limits[key])
                        except Exception:
                            pass
        resources = []
        for idx, (resource_name, display_name, resource_type) in enumerate([
            ("nvidia.com/gpu", "A100 Shared Slot", "shared"),
            ("nvidia.com/mig-1g.10gb", "A100 MIG 1g.10gb", "mig"),
        ], start=1):
            cap = int(capacity.get(resource_name, 0))
            alloc = int(allocatable.get(resource_name, 0))
            available = max(cap - used[resource_name], 0)
            resources.append({
                "id": idx,
                "node_name": "g01",
                "name": display_name,
                "resource_name": resource_name,
                "capacity": cap,
                "used": used[resource_name],
                "available": available,
                "allocatable": alloc,
                "product": "NVIDIA-A100-80GB-PCIe",
                "type": resource_type,
                "status": "available" if available > 0 else "gpu_present_not_advertised" if cap == 0 else "unavailable",
            })
        return resources
    except Exception as e:
        logger.error(f"GPU inventory error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/billing-events")
def get_billing_events(db: Session = Depends(get_db)):
    events = db.query(models.BillingEvent).order_by(models.BillingEvent.id.desc()).all()
    return [
        {"id": e.id, "user_id": e.user_id, "instance_id": e.instance_id, "amount": e.amount, "event_type": e.event_type, "timestamp": e.timestamp.isoformat() if e.timestamp else None}
        for e in events
    ]


@app.get("/api/billing-events/user/{user_id}")
def get_user_billing_events(user_id: int, db: Session = Depends(get_db)):
    events = db.query(models.BillingEvent).filter(models.BillingEvent.user_id == user_id).order_by(models.BillingEvent.id).all()
    return [
        {"id": e.id, "user_id": e.user_id, "instance_id": e.instance_id, "amount": e.amount, "event_type": e.event_type, "timestamp": e.timestamp.isoformat() if e.timestamp else None}
        for e in events
    ]


@app.post("/api/billing-events")
def create_billing_event(payload: dict, db: Session = Depends(get_db)):
    event = models.BillingEvent(
        user_id=payload.get("user_id"),
        instance_id=payload.get("instance_id"),
        amount=float(payload.get("amount", 0)),
        event_type=payload.get("event_type", "manual"),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return {"id": event.id, "user_id": event.user_id, "instance_id": event.instance_id, "amount": event.amount, "event_type": event.event_type, "timestamp": event.timestamp.isoformat() if event.timestamp else None}


@app.get("/api/k8s/all-pods")
def get_all_pods():
    return [raw_pod_to_dict(p) for p in k8s_raw_list_pods()]

@app.get("/api/k8s/gpu-pods")
def get_gpu_pods():
    result = []
    for pod in k8s_raw_list_pods():
        uses_gpu = False
        for container in pod.get("spec", {}).get("containers", []) or []:
            limits = container.get("resources", {}).get("limits", {}) or {}
            if any(k.startswith("nvidia.com/") for k in limits.keys()):
                uses_gpu = True
        if uses_gpu or (pod.get("metadata", {}).get("labels", {}) or {}).get("app") == "gpu-tenant-instance":
            result.append(raw_pod_to_dict(pod))
    return result

@app.get("/api/k8s/node-summary/{node_name}")
def get_node_summary(node_name: str):
    node_info = get_node_resources(node_name)
    pods = k8s_raw_list_pods(field_selector=f"spec.nodeName={node_name}")
    return {
        "node": node_name,
        "resources": node_info.get("gpu_resources", {}),
        "labels": node_info.get("labels", {}),
        "pod_count": len(pods),
        "gpu_pods": len([p for p in pods if (p.get("metadata", {}).get("labels", {}) or {}).get("app") == "gpu-tenant-instance"]),
    }

@app.get("/api/k8s/pod-status/{namespace}/{pod_name}")
def get_pod_status(namespace: str, pod_name: str):
    pod = k8s_raw_request("GET", f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods/{urllib.parse.quote(pod_name)}")
    return raw_pod_to_dict(pod)

@app.get("/api/k8s/pod-exec/{namespace}/{pod_name}")
def pod_exec_placeholder(namespace: str, pod_name: str):
    return {"namespace": namespace, "pod_name": pod_name, "output": "Exec endpoint placeholder. GPU status exec is not enabled in MVP."}


@app.delete("/api/k8s/delete-pod/{namespace}/{pod_name}")
def delete_gpu_pod(namespace: str, pod_name: str):
    return delete_pod(namespace, pod_name)


@app.post("/api/k8s/create-pod")
def create_gpu_pod_placeholder(payload: dict):
    return {"message": "Use /api/instances for managed GPU rental pods.", "received": payload}


# =========================
# Storage Management
# =========================

@app.get("/api/storage-volumes", response_model=list[StorageVolumeResponse])
def get_storage_volumes(db: Session = Depends(get_db)):
    return db.query(models.StorageVolume).order_by(models.StorageVolume.id).all()


@app.post("/api/storage-volumes", response_model=StorageVolumeResponse)
def create_storage_volume(volume: StorageVolumeCreate, db: Session = Depends(get_db)):
    db_volume = models.StorageVolume(**volume.model_dump())
    try:
        db.add(db_volume)
        db.commit()
        db.refresh(db_volume)
        return db_volume
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Storage volume creation failed: {e}")
        raise HTTPException(status_code=400, detail="Storage volume creation failed")


@app.get("/api/storage-volumes/{volume_id}", response_model=StorageVolumeResponse)
def get_storage_volume(volume_id: int, db: Session = Depends(get_db)):
    volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == volume_id).first()
    if not volume:
        raise HTTPException(status_code=404, detail="Storage volume not found")
    return volume


@app.put("/api/storage-volumes/{volume_id}", response_model=StorageVolumeResponse)
def update_storage_volume(volume_id: int, payload: StorageVolumeUpdate, db: Session = Depends(get_db)):
    volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == volume_id).first()
    if not volume:
        raise HTTPException(status_code=404, detail="Storage volume not found")
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(volume, key, value)
    try:
        db.commit()
        db.refresh(volume)
        return volume
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Storage volume update failed: {e}")
        raise HTTPException(status_code=400, detail="Storage volume update failed")


@app.delete("/api/storage-volumes/{volume_id}")
def delete_storage_volume(volume_id: int, db: Session = Depends(get_db)):
    volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == volume_id).first()
    if not volume:
        raise HTTPException(status_code=404, detail="Storage volume not found")
    
    try:
        db.delete(volume)
        db.commit()
        return {"message": "Storage volume deleted", "id": volume_id}
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Storage volume deletion failed: {e}")
        raise HTTPException(status_code=400, detail="Storage volume deletion failed")


# =========================
# User Storage (Quotas & Folders)
# =========================

@app.get("/api/user-storages", response_model=list[UserStorageResponse])
def get_user_storages(project_id: int | None = None, user_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(models.UserStorage)
    if project_id is not None:
        query = query.filter(models.UserStorage.project_id == project_id)
    if user_id is not None:
        query = query.filter(models.UserStorage.user_id == user_id)
    return query.order_by(models.UserStorage.id).all()


@app.get("/api/user-storages/user/{user_id}", response_model=list[UserStorageResponse])
def get_user_storage_by_user(user_id: int, db: Session = Depends(get_db)):
    return db.query(models.UserStorage).filter(models.UserStorage.user_id == user_id).all()


@app.post("/api/user-storages", response_model=UserStorageResponse)
def create_user_storage(storage: UserStorageCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == storage.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    project_id = storage.project_id or user.project_id
    if project_id is None:
        raise HTTPException(status_code=400, detail="User has no project_id; assign user to project first")
    project = validate_project_exists(db, project_id)

    if user.project_id != project.id:
        raise HTTPException(status_code=403, detail="User does not belong to selected project")

    volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == storage.volume_id).first()
    if not volume:
        raise HTTPException(status_code=404, detail="Storage volume not found")

    if storage.quota_gb <= 0:
        raise HTTPException(status_code=400, detail="quota_gb must be positive")

    if volume.used_capacity_gb + storage.quota_gb > volume.total_capacity_gb:
        raise HTTPException(status_code=409, detail="Storage volume capacity exceeded")

    current_project_storage = get_project_storage_usage_gb(db, project.id)
    if project.max_storage_gb > 0 and current_project_storage + storage.quota_gb > project.max_storage_gb:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Project '{project.name}' storage quota exceeded.",
                "max_allowed_gb": project.max_storage_gb,
                "currently_allocated_gb": current_project_storage,
                "requested_gb": storage.quota_gb
            }
        )

    existing = db.query(models.UserStorage).filter(
        models.UserStorage.user_id == storage.user_id,
        models.UserStorage.volume_id == storage.volume_id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="User already has storage on this volume")

    data = storage.model_dump()
    data["project_id"] = project.id
    db_storage = models.UserStorage(**data)

    try:
        db.add(db_storage)
        volume.used_capacity_gb += storage.quota_gb
        db.commit()
        db.refresh(db_storage)
        return db_storage
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"User storage creation failed: {e}")
        raise HTTPException(status_code=400, detail="User storage creation failed")

@app.delete("/api/user-storages/{storage_id}")
def delete_user_storage(storage_id: int, db: Session = Depends(get_db)):
    storage = db.query(models.UserStorage).filter(models.UserStorage.id == storage_id).first()
    if not storage:
        raise HTTPException(status_code=404, detail="User storage not found")
    
    try:
        # Update volume used capacity
        volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == storage.volume_id).first()
        if volume:
            volume.used_capacity_gb -= storage.quota_gb
        
        db.delete(storage)
        db.commit()
        return {"message": "User storage deleted", "id": storage_id}
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"User storage deletion failed: {e}")
        raise HTTPException(status_code=400, detail="User storage deletion failed")


@app.put("/api/user-storages/{storage_id}/quota")
def update_user_storage_quota(storage_id: int, payload: dict, db: Session = Depends(get_db)):
    storage = db.query(models.UserStorage).filter(models.UserStorage.id == storage_id).first()
    if not storage:
        raise HTTPException(status_code=404, detail="User storage not found")

    new_quota = payload.get("quota_gb")
    if new_quota is None:
        raise HTTPException(status_code=400, detail="quota_gb is required")
    new_quota = int(new_quota)
    if new_quota <= 0:
        raise HTTPException(status_code=400, detail="quota_gb must be positive")

    try:
        volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == storage.volume_id).first()
        delta = new_quota - storage.quota_gb
        if volume and volume.used_capacity_gb + delta > volume.total_capacity_gb:
            raise HTTPException(status_code=409, detail="Storage volume capacity exceeded")

        if storage.project_id:
            project = validate_project_exists(db, storage.project_id)
            current_project_storage = get_project_storage_usage_gb(db, storage.project_id)
            if project.max_storage_gb > 0 and current_project_storage + delta > project.max_storage_gb:
                raise HTTPException(status_code=409, detail="Project storage quota exceeded")

        if volume:
            volume.used_capacity_gb = volume.used_capacity_gb + delta

        storage.quota_gb = new_quota
        db.commit()
        db.refresh(storage)

        return {
            "message": "Quota updated",
            "id": storage_id,
            "new_quota_gb": new_quota
        }
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Quota update failed: {e}")
        raise HTTPException(status_code=400, detail="Quota update failed")
