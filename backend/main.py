import logging
import uuid

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
    UserResponse,
    PlanCreate,
    PlanResponse,
    InstanceCreate,
    InstanceResponse,
    StorageVolumeCreate,
    StorageVolumeResponse,
    UserStorageCreate,
    UserStorageResponse,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes config.")
except config.ConfigException:
    logger.warning("Could not load in-cluster Kubernetes config.")

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
# Users
# =========================

@app.post("/api/users", response_model=UserResponse)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    db_user = models.User(**user.model_dump())

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
def get_users(db: Session = Depends(get_db)):
    return db.query(models.User).order_by(models.User.id).all()


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


def calculate_resource_usage(v1: client.CoreV1Api, node_name: str, resource_name: str) -> dict:
    node = v1.read_node(name=node_name)
    allocatable = node.status.allocatable or {}
    capacity = int(allocatable.get(resource_name, "0"))
    pods = v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}")
    used = 0
    for pod in pods.items:
        if pod.status.phase in ["Succeeded", "Failed"]:
            continue
        for container in pod.spec.containers:
            limits = container.resources.limits if container.resources and container.resources.limits else {}
            if resource_name in limits:
                used += int(limits[resource_name])
    free = capacity - used
    return {"capacity": capacity, "used": used, "free": free}


@app.post("/api/instances", response_model=InstanceResponse)
def create_instance(instance: InstanceCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == instance.user_id).first()
    plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == instance.plan_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    namespace = "gpu-rental-system"
    node_name = "g01"
    resource_name = plan.k8s_resource_name
    requested_count = int(plan.resource_count)
    safe_image = normalize_image_name(instance.image)

    try:
        v1 = client.CoreV1Api()
        usage = calculate_resource_usage(v1, node_name, resource_name)

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

        short_uuid = uuid.uuid4().hex[:6]
        pod_name = f"gpu-tenant-u{user.id}-{short_uuid}"

        pod_manifest = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                labels={
                    "app": "gpu-tenant-instance",
                    "user_id": str(user.id),
                    "plan_id": str(plan.id),
                    "billing": "true",
                },
            ),
            spec=client.V1PodSpec(
                node_name=node_name,
                restart_policy="Never",
                containers=[
                    client.V1Container(
                        name="ai-workspace",
                        image=safe_image,
                        command=["sleep", "infinity"],
                        resources=client.V1ResourceRequirements(
                            limits={resource_name: str(requested_count)}
                        ),
                    )
                ],
            ),
        )

        v1.create_namespaced_pod(namespace=namespace, body=pod_manifest)
        logger.info(f"Created pod {pod_name} using {resource_name}:{requested_count}")

    except HTTPException:
        raise
    except client.exceptions.ApiException as e:
        logger.error(f"Kubernetes pod creation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Kubernetes pod creation failed: {e.reason}")
    except Exception as e:
        logger.error(f"Unexpected instance creation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    db_instance = models.Instance(
        user_id=user.id,
        plan_id=plan.id,
        pod_name=pod_name,
        namespace=namespace,
        pvc_name="mock-pvc-for-now",
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
        v1 = client.CoreV1Api()
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

    v1 = client.CoreV1Api()

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

        usage = calculate_resource_usage(v1, node_name, resource_name)

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


# =========================
# Kubernetes Discovery
# =========================

@app.get("/api/k8s/node-resources/{node_name}")
def get_node_resources(node_name: str):
    try:
        v1 = client.CoreV1Api()
        node = v1.read_node(name=node_name)

        capacity = node.status.capacity or {}
        allocatable = node.status.allocatable or {}
        labels = node.metadata.labels or {}

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
    except client.exceptions.ApiException as e:
        logger.error(f"Kubernetes API error: {e}")
        raise HTTPException(status_code=e.status, detail=f"Kubernetes API error: {e.reason}")
    except Exception as e:
        logger.error(f"Failed to fetch node resources: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/pods/{namespace}/{pod_name}/logs")
def get_pod_logs(namespace: str, pod_name: str, tail_lines: int = 100):
    try:
        logs = client.CoreV1Api().read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=tail_lines)
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
    try:
        nodes = client.CoreV1Api().list_node().items
        return [node_to_dict(n) for n in nodes]
    except client.exceptions.ApiException as e:
        raise HTTPException(status_code=e.status, detail=f"Kubernetes error: {e.reason}")


@app.get("/api/namespaces")
def get_namespaces():
    try:
        namespaces = client.CoreV1Api().list_namespace().items
        return [
            {"name": ns.metadata.name, "status": ns.status.phase, "created_at": ns.metadata.creation_timestamp.isoformat() if ns.metadata.creation_timestamp else None}
            for ns in namespaces
        ]
    except client.exceptions.ApiException as e:
        raise HTTPException(status_code=e.status, detail=f"Kubernetes error: {e.reason}")


@app.get("/api/pods")
def get_pods(namespace: str = None):
    try:
        v1 = client.CoreV1Api()
        if namespace:
            pods = v1.list_namespaced_pod(namespace=namespace).items
        else:
            pods = v1.list_pod_for_all_namespaces().items
        return [pod_to_dict(p) for p in pods]
    except client.exceptions.ApiException as e:
        raise HTTPException(status_code=e.status, detail=f"Kubernetes error: {e.reason}")


@app.get("/api/services")
def get_services(namespace: str = None):
    try:
        v1 = client.CoreV1Api()
        if namespace:
            services = v1.list_namespaced_service(namespace=namespace).items
        else:
            services = v1.list_service_for_all_namespaces().items
        return [
            {
                "name": svc.metadata.name,
                "namespace": svc.metadata.namespace,
                "type": svc.spec.type,
                "cluster_ip": svc.spec.cluster_ip,
                "ports": [{"name": p.name, "port": p.port, "target_port": str(p.target_port), "node_port": p.node_port, "protocol": p.protocol} for p in (svc.spec.ports or [])],
            }
            for svc in services
        ]
    except client.exceptions.ApiException as e:
        raise HTTPException(status_code=e.status, detail=f"Kubernetes error: {e.reason}")


@app.delete("/api/pods/{namespace}/{pod_name}")
def delete_pod(namespace: str, pod_name: str):
    try:
        client.CoreV1Api().delete_namespaced_pod(name=pod_name, namespace=namespace)
        return {"message": "Pod deleted", "namespace": namespace, "pod_name": pod_name}
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return {"message": "Pod already missing", "namespace": namespace, "pod_name": pod_name}
        raise HTTPException(status_code=e.status, detail=f"Kubernetes error: {e.reason}")


@app.get("/api/gpu-inventory")
def get_gpu_inventory():
    try:
        v1 = client.CoreV1Api()
        node = v1.read_node(name="g01")

        allocatable = node.status.allocatable or {}
        capacity = node.status.capacity or {}

        pods = v1.list_namespaced_pod(namespace="gpu-rental-system")
        used = {"nvidia.com/gpu": 0, "nvidia.com/mig-1g.10gb": 0}

        for pod in pods.items:
            for container in pod.spec.containers:
                limits = container.resources.limits
                if limits:
                    if "nvidia.com/gpu" in limits:
                        used["nvidia.com/gpu"] += int(limits["nvidia.com/gpu"])
                    if "nvidia.com/mig-1g.10gb" in limits:
                        used["nvidia.com/mig-1g.10gb"] += int(limits["nvidia.com/mig-1g.10gb"])

        resources = [
            {
                "id": 1,
                "node_name": "g01",
                "name": "A100 Shared Slot",
                "resource_name": "nvidia.com/gpu",
                "capacity": int(capacity.get("nvidia.com/gpu", 0)),
                "used": used["nvidia.com/gpu"],
                "available": int(capacity.get("nvidia.com/gpu", 0)) - used["nvidia.com/gpu"],
                "allocatable": int(allocatable.get("nvidia.com/gpu", 0)),
                "product": "NVIDIA-A100-80GB-PCIe-SHARED",
                "type": "shared",
                "status": "available" if (int(capacity.get("nvidia.com/gpu", 0)) - used["nvidia.com/gpu"]) > 0 else "unavailable"
            },
            {
                "id": 2,
                "node_name": "g01",
                "name": "A100 MIG 1g.10gb",
                "resource_name": "nvidia.com/mig-1g.10gb",
                "capacity": int(capacity.get("nvidia.com/mig-1g.10gb", 0)),
                "used": used["nvidia.com/mig-1g.10gb"],
                "available": int(capacity.get("nvidia.com/mig-1g.10gb", 0)) - used["nvidia.com/mig-1g.10gb"],
                "allocatable": int(allocatable.get("nvidia.com/mig-1g.10gb", 0)),
                "product": "NVIDIA-A100-80GB-PCIe-MIG-1g.10gb",
                "type": "mig",
                "status": "available" if (int(capacity.get("nvidia.com/mig-1g.10gb", 0)) - used["nvidia.com/mig-1g.10gb"]) > 0 else "unavailable"
            }
        ]
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
    pods = client.CoreV1Api().list_pod_for_all_namespaces().items
    return [pod_to_dict(p) for p in pods]


@app.get("/api/k8s/gpu-pods")
def get_gpu_pods():
    pods = client.CoreV1Api().list_pod_for_all_namespaces().items
    result = []
    for pod in pods:
        uses_gpu = False
        for container in pod.spec.containers:
            limits = container.resources.limits if container.resources and container.resources.limits else {}
            if any(k.startswith("nvidia.com/") for k in limits.keys()):
                uses_gpu = True
        if uses_gpu or (pod.metadata.labels or {}).get("app") == "gpu-tenant-instance":
            result.append(pod_to_dict(pod))
    return result


@app.get("/api/k8s/node-summary/{node_name}")
def get_node_summary(node_name: str):
    node_info = get_node_resources(node_name)
    pods = client.CoreV1Api().list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}").items
    return {
        "node": node_name,
        "resources": node_info.get("gpu_resources", {}),
        "labels": node_info.get("labels", {}),
        "pod_count": len(pods),
        "gpu_pods": len([p for p in pods if (p.metadata.labels or {}).get("app") == "gpu-tenant-instance"]),
    }


@app.get("/api/k8s/pod-status/{namespace}/{pod_name}")
def get_pod_status(namespace: str, pod_name: str):
    try:
        pod = client.CoreV1Api().read_namespaced_pod(name=pod_name, namespace=namespace)
        return pod_to_dict(pod)
    except client.exceptions.ApiException as e:
        raise HTTPException(status_code=e.status, detail=f"Kubernetes error: {e.reason}")


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
def get_user_storages(db: Session = Depends(get_db)):
    return db.query(models.UserStorage).order_by(models.UserStorage.id).all()


@app.get("/api/user-storages/user/{user_id}", response_model=list[UserStorageResponse])
def get_user_storage_by_user(user_id: int, db: Session = Depends(get_db)):
    return db.query(models.UserStorage).filter(models.UserStorage.user_id == user_id).all()


@app.post("/api/user-storages", response_model=UserStorageResponse)
def create_user_storage(storage: UserStorageCreate, db: Session = Depends(get_db)):
    # Verify user exists
    user = db.query(models.User).filter(models.User.id == storage.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Verify volume exists
    volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == storage.volume_id).first()
    if not volume:
        raise HTTPException(status_code=404, detail="Storage volume not found")
    
    # Check if user already has storage on this volume
    existing = db.query(models.UserStorage).filter(
        models.UserStorage.user_id == storage.user_id,
        models.UserStorage.volume_id == storage.volume_id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="User already has storage on this volume")
    
    db_storage = models.UserStorage(**storage.model_dump())
    
    try:
        db.add(db_storage)
        db.commit()
        db.refresh(db_storage)
        
        # Update volume used capacity
        volume.used_capacity_gb += storage.quota_gb
        db.commit()
        
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
    
    try:
        # Update volume used capacity
        volume = db.query(models.StorageVolume).filter(models.StorageVolume.id == storage.volume_id).first()
        if volume:
            volume.used_capacity_gb = volume.used_capacity_gb - storage.quota_gb + new_quota
        
        storage.quota_gb = new_quota
        db.commit()
        db.refresh(storage)
        
        return {
            "message": "Quota updated",
            "id": storage_id,
            "new_quota_gb": new_quota
        }
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Quota update failed: {e}")
        raise HTTPException(status_code=400, detail="Quota update failed")
