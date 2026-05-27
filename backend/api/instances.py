import uuid, logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from database import get_db
import models
from schemas import InstanceCreate, InstanceResponse, InstancePortCreate, InstancePortResponse
from services.kubernetes_svc import k8s_raw_create_pod, k8s_raw_delete_pod, k8s_raw_read_pod, k8s_raw_create_service, k8s_raw_read_service, k8s_raw_delete_service, calculate_resource_usage, get_node_external_ip, build_launch_url, build_pod_manifest, infer_app_type_from_port, normalize_image_name, NODE_NAME, NAMESPACE

router = APIRouter(prefix="/api/instances", tags=["Instances"])
logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_IMAGES = {
    "ubuntu:22.04", "docker.io/library/ubuntu:22.04",
    "jupyter/minimal-notebook:latest", "docker.io/jupyter/minimal-notebook:latest",
    "nvidia/cuda:12.0-base-ubuntu22.04", "docker.io/nvidia/cuda:12.0-base-ubuntu22.04",
    "nvidia/cuda:11.8-base-ubuntu22.04", "docker.io/nvidia/cuda:11.8-base-ubuntu22.04",
    "pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime", "docker.io/pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime",
    "tensorflow/tensorflow:2.13.0-gpu", "docker.io/tensorflow/tensorflow:2.13.0-gpu",
}

def validate_image(db: Session, image: str) -> str:
    normalized = normalize_image_name(image)
    if image in DEFAULT_ALLOWED_IMAGES or normalized in DEFAULT_ALLOWED_IMAGES:
        return normalized
    exists = db.query(models.AllowedImage).filter(models.AllowedImage.image_url.in_([image, normalized])).first()
    if exists: return normalized
    raise HTTPException(status_code=400, detail=f"Image is not allowed: {image}. Add it via /api/images first.")

def _running_gpu_usage(db: Session, project_id: int) -> int:
    current = db.query(models.Instance).filter(models.Instance.project_id == project_id, models.Instance.status.in_([models.InstanceStatusEnum.RUNNING, models.InstanceStatusEnum.PENDING])).all()
    total = 0
    for inst in current:
        plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == inst.plan_id).first()
        if plan: total += int(plan.resource_count or 0)
    return total

@router.get("", response_model=list[InstanceResponse])
def get_instances(db: Session = Depends(get_db)):
    return db.query(models.Instance).order_by(models.Instance.id).all()

@router.post("", response_model=InstanceResponse)
def create_instance(instance: InstanceCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == instance.user_id).first()
    plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == instance.plan_id).first()
    project = db.query(models.Project).filter(models.Project.id == instance.project_id).first()
    if not user or not plan or not project: raise HTTPException(status_code=404, detail="User/Plan/Project not found")
    if user.project_id is not None and user.project_id != project.id: raise HTTPException(status_code=403, detail="User does not belong to the requested project")
    requested_count = int(plan.resource_count)
    current_gpu = _running_gpu_usage(db, project.id)
    if not (project.id == 1 and project.max_gpu_count == 0) and current_gpu + requested_count > project.max_gpu_count:
        raise HTTPException(status_code=409, detail={"message":f"Project '{project.name}' GPU quota exceeded.", "max_allowed":project.max_gpu_count, "currently_using":current_gpu, "requested":requested_count})
    safe_image = validate_image(db, instance.image)
    usage = calculate_resource_usage(NODE_NAME, plan.k8s_resource_name)
    if usage["free"] < requested_count: raise HTTPException(status_code=409, detail=f"No available Kubernetes capacity for {plan.k8s_resource_name}")
    pod_name = f"gpu-p{project.id}-u{user.id}-{uuid.uuid4().hex[:6]}"
    db_instance = models.Instance(user_id=user.id, project_id=project.id, plan_id=plan.id, pod_name=pod_name, namespace=NAMESPACE, status=models.InstanceStatusEnum.PENDING, pvc_name=None, app_type=instance.app_type or "terminal", image=safe_image, cpu_cores=instance.cpu_cores, memory_gb=instance.memory_gb, shm_gb=instance.shm_gb, storage_id=instance.storage_id)
    try:
        db.add(db_instance); db.commit(); db.refresh(db_instance)
    except SQLAlchemyError as e:
        db.rollback(); logger.error(e); raise HTTPException(status_code=500, detail="Database error saving instance")
    try:
        manifest, pvc_name = build_pod_manifest(db, project, user, plan, db_instance, safe_image, pod_name)
        k8s_raw_create_pod(namespace=NAMESPACE, manifest=manifest)
    except Exception as e:
        logger.error(f"Pod creation failed: {e}")
        db.delete(db_instance); db.commit()
        raise HTTPException(status_code=500, detail=f"Kubernetes pod creation failed: {e}")
    db_instance.status = models.InstanceStatusEnum.RUNNING
    db_instance.pvc_name = pvc_name or ""
    db.commit(); db.refresh(db_instance)
    return db_instance

@router.delete("/{instance_id}")
def delete_instance(instance_id: int, db: Session = Depends(get_db)):
    inst = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if not inst: raise HTTPException(status_code=404, detail="Instance not found")
    try: k8s_raw_delete_pod(inst.namespace, inst.pod_name)
    except HTTPException as e:
        if e.status_code != 404: raise
    inst.status = models.InstanceStatusEnum.DELETED; db.commit()
    return {"message":"Instance deleted", "id":inst.id, "pod_name":inst.pod_name, "status":inst.status.value}

@router.post("/{instance_id}/action")
def instance_action(instance_id: int, payload: dict, db: Session = Depends(get_db)):
    inst = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if not inst: raise HTTPException(status_code=404, detail="Instance not found")
    action = payload.get("action", "").lower().strip()
    if action not in ["start", "stop", "restart"]: raise HTTPException(status_code=400, detail="Invalid action")
    if action in ["stop", "restart"]:
        try: k8s_raw_delete_pod(inst.namespace, inst.pod_name)
        except HTTPException as e:
            if e.status_code != 404: raise
        inst.status = models.InstanceStatusEnum.STOPPED; db.commit()
        if action == "stop": return {"message":"Instance stopped", "id":inst.id, "pod_name":inst.pod_name, "status":inst.status.value}
    if action in ["start", "restart"]:
        plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == inst.plan_id).first()
        user = db.query(models.User).filter(models.User.id == inst.user_id).first()
        project = db.query(models.Project).filter(models.Project.id == inst.project_id).first()
        if not plan or not user or not project: raise HTTPException(status_code=404, detail="Plan/user/project not found for instance")
        usage = calculate_resource_usage(NODE_NAME, plan.k8s_resource_name)
        if usage["free"] < int(plan.resource_count): raise HTTPException(status_code=409, detail={"message":f"No available capacity for {plan.k8s_resource_name}", **usage, "requested":int(plan.resource_count)})
        safe_image = validate_image(db, inst.image or "jupyter/minimal-notebook:latest")
        manifest, pvc_name = build_pod_manifest(db, project, user, plan, inst, safe_image, inst.pod_name)
        k8s_raw_create_pod(namespace=inst.namespace, manifest=manifest)
        inst.pvc_name = pvc_name or inst.pvc_name
        inst.status = models.InstanceStatusEnum.RUNNING
        node_ip = get_node_external_ip(NODE_NAME)
        for port in db.query(models.InstancePort).filter(models.InstancePort.instance_id == inst.id, models.InstancePort.status == "open").all():
            app_type = infer_app_type_from_port(port.target_port, inst.app_type)
            port.launch_url = build_launch_url(node_ip, port.node_port, app_type, inst.pod_name)
        db.commit()
        return {"message":"Instance started", "id":inst.id, "pod_name":inst.pod_name, "status":inst.status.value}

@router.get("/{instance_id}/ports", response_model=list[InstancePortResponse])
def list_instance_ports(instance_id: int, db: Session = Depends(get_db)):
    if not db.query(models.Instance).filter(models.Instance.id == instance_id).first(): raise HTTPException(status_code=404, detail="Instance not found")
    return db.query(models.InstancePort).filter(models.InstancePort.instance_id == instance_id).all()

@router.post("/{instance_id}/ports", response_model=InstancePortResponse)
def open_instance_port(instance_id: int, payload: InstancePortCreate, db: Session = Depends(get_db)):
    inst = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if not inst: raise HTTPException(status_code=404, detail="Instance not found")
    target_port, exposed_port, protocol = int(payload.target_port or payload.port), int(payload.port), (payload.protocol or "TCP").upper()
    service_name = f"{inst.pod_name}-port-{exposed_port}".lower().replace("_", "-")[:63]
    app_type = infer_app_type_from_port(target_port, inst.app_type)
    service = __import__('kubernetes').client.V1Service(metadata=__import__('kubernetes').client.V1ObjectMeta(name=service_name, namespace=inst.namespace, labels={"app":"gpu-tenant-instance-port", "instance_id":str(inst.id), "pod_name":inst.pod_name}), spec=__import__('kubernetes').client.V1ServiceSpec(type="NodePort", selector={"app":"gpu-tenant-instance", "pod_name":inst.pod_name}, ports=[__import__('kubernetes').client.V1ServicePort(name=f"port-{exposed_port}", port=exposed_port, target_port=target_port, protocol=protocol)]))
    try:
        created = k8s_raw_create_service(inst.namespace, service)
    except HTTPException as e:
        if e.status_code == 409: created = k8s_raw_read_service(inst.namespace, service_name)
        else: raise
    ports = created.get("spec", {}).get("ports", []) or []
    node_port = ports[0].get("nodePort") if ports else None
    if not node_port: raise HTTPException(status_code=500, detail="Kubernetes failed to assign a NodePort")
    launch_url = build_launch_url(get_node_external_ip(NODE_NAME), node_port, app_type, inst.pod_name)
    existing = db.query(models.InstancePort).filter(models.InstancePort.instance_id == instance_id, models.InstancePort.port == exposed_port, models.InstancePort.status == "open").first()
    if existing:
        existing.target_port, existing.node_port, existing.protocol, existing.service_name, existing.launch_url = target_port, node_port, protocol, service_name, launch_url
        db.commit(); db.refresh(existing); return existing
    row = models.InstancePort(instance_id=instance_id, port=exposed_port, target_port=target_port, node_port=node_port, protocol=protocol, service_name=service_name, launch_url=launch_url, status="open")
    db.add(row); db.commit(); db.refresh(row); return row

@router.delete("/{instance_id}/ports/{port_id}")
def close_instance_port(instance_id: int, port_id: int, db: Session = Depends(get_db)):
    inst = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if not inst: raise HTTPException(status_code=404, detail="Instance not found")
    row = db.query(models.InstancePort).filter(models.InstancePort.id == port_id, models.InstancePort.instance_id == instance_id).first()
    if not row: raise HTTPException(status_code=404, detail="Port record not found")
    try: k8s_raw_delete_service(inst.namespace, row.service_name)
    except HTTPException as e:
        if e.status_code != 404: raise
    row.status = "closed"; db.commit(); return {"message":"Port closed", "id":row.id, "port":row.port}

@router.get("/{instance_id}/launch")
def get_instance_launch(instance_id: int, db: Session = Depends(get_db)):
    inst = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if not inst: raise HTTPException(status_code=404, detail="Instance not found")
    ports = db.query(models.InstancePort).filter(models.InstancePort.instance_id == instance_id, models.InstancePort.status == "open").all()
    return {"instance_id":inst.id, "pod_name":inst.pod_name, "status":inst.status.value if hasattr(inst.status, 'value') else str(inst.status), "ports":[{"id":p.id, "port":p.port, "target_port":p.target_port, "node_port":p.node_port, "launch_url":p.launch_url, "status":p.status} for p in ports]}
