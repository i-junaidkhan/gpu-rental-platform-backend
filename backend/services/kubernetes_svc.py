import os, json, ssl, urllib.request, urllib.parse, urllib.error, logging, uuid
from typing import Optional, Tuple
from fastapi import HTTPException
from kubernetes import client, config
from sqlalchemy.orm import Session
import models

logger = logging.getLogger(__name__)
NAMESPACE = "gpu-rental-system"
NODE_NAME = "g01"
STORAGE_BASE = os.environ.get("STORAGE_BASE_PATH", "/mnt/gpu-rental-storage")
PVC_NAME = "gpu-rental-storage-pvc"

# ---------- raw Kubernetes API; Python kubernetes client auth is unreliable in this cluster ----------
def _k8s_raw_base():
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    return f"https://{host}:{port}"

def _k8s_raw_token():
    with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
        return f.read().strip()

def _k8s_raw_ssl_context():
    ctx = ssl.create_default_context()
    ctx.load_verify_locations("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    return ctx

def k8s_raw_request(method: str, path: str, body: Optional[dict] = None) -> dict:
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

def k8s_raw_request_text(method: str, path: str) -> str:
    url = _k8s_raw_base() + path
    req = urllib.request.Request(url, method=method.upper())
    req.add_header("Authorization", f"Bearer {_k8s_raw_token()}")
    try:
        with urllib.request.urlopen(req, context=_k8s_raw_ssl_context(), timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=e.code, detail=f"Kubernetes error: {error_body}")

def q(x: str) -> str:
    return urllib.parse.quote(x, safe="")

def k8s_raw_list_nodes(): return k8s_raw_request("GET", "/api/v1/nodes").get("items", [])
def k8s_raw_read_node(name: str): return k8s_raw_request("GET", f"/api/v1/nodes/{q(name)}")
def k8s_raw_list_namespaces(): return k8s_raw_request("GET", "/api/v1/namespaces").get("items", [])

def k8s_raw_list_pods(namespace: Optional[str] = None, field_selector: Optional[str] = None):
    path = f"/api/v1/namespaces/{q(namespace)}/pods" if namespace else "/api/v1/pods"
    if field_selector:
        path += "?fieldSelector=" + urllib.parse.quote(field_selector)
    return k8s_raw_request("GET", path).get("items", [])

def k8s_raw_list_services(namespace: Optional[str] = None):
    path = f"/api/v1/namespaces/{q(namespace)}/services" if namespace else "/api/v1/services"
    return k8s_raw_request("GET", path).get("items", [])

def k8s_raw_read_pod(namespace: str, pod_name: str):
    return k8s_raw_request("GET", f"/api/v1/namespaces/{q(namespace)}/pods/{q(pod_name)}")

def k8s_raw_read_service(namespace: str, service_name: str):
    return k8s_raw_request("GET", f"/api/v1/namespaces/{q(namespace)}/services/{q(service_name)}")

def k8s_raw_create_pod(namespace: str, manifest):
    body = client.ApiClient().sanitize_for_serialization(manifest)
    return k8s_raw_request("POST", f"/api/v1/namespaces/{q(namespace)}/pods", body=body)

def k8s_raw_delete_pod(namespace: str, pod_name: str):
    return k8s_raw_request("DELETE", f"/api/v1/namespaces/{q(namespace)}/pods/{q(pod_name)}", body={})

def k8s_raw_create_service(namespace: str, service):
    body = client.ApiClient().sanitize_for_serialization(service)
    return k8s_raw_request("POST", f"/api/v1/namespaces/{q(namespace)}/services", body=body)

def k8s_raw_delete_service(namespace: str, service_name: str):
    return k8s_raw_request("DELETE", f"/api/v1/namespaces/{q(namespace)}/services/{q(service_name)}", body={})

# Keep client for serialization and fallback logs only.
k8s_v1 = None
try:
    try:
        config.load_incluster_config()
        k8s_v1 = client.CoreV1Api()
    except Exception:
        config.load_kube_config()
        k8s_v1 = client.CoreV1Api()
except Exception as e:
    logger.warning(f"Could not initialize Kubernetes Python client: {e}")

# ---------- serializers ----------
def _raw_ts(meta): return (meta or {}).get("creationTimestamp")
def _raw_node_conditions(node): return {c.get("type"): c.get("status") for c in node.get("status", {}).get("conditions", [])}

def raw_node_to_dict(node: dict) -> dict:
    meta, status = node.get("metadata", {}), node.get("status", {})
    labels, capacity, allocatable = meta.get("labels", {}) or {}, status.get("capacity", {}) or {}, status.get("allocatable", {}) or {}
    conditions = _raw_node_conditions(node)
    return {
        "name": meta.get("name"), "status": "Ready" if conditions.get("Ready") == "True" else "NotReady",
        "ready": conditions.get("Ready") == "True", "roles": labels,
        "cpu": allocatable.get("cpu") or capacity.get("cpu"), "memory": allocatable.get("memory") or capacity.get("memory"),
        "gpu": allocatable.get("nvidia.com/gpu", capacity.get("nvidia.com/gpu", "0")),
        "mig_1g_10gb": allocatable.get("nvidia.com/mig-1g.10gb", capacity.get("nvidia.com/mig-1g.10gb", "0")),
        "capacity": capacity, "allocatable": allocatable, "labels": labels,
        "internal_ip": next((a.get("address") for a in status.get("addresses", []) if a.get("type") == "InternalIP"), None),
        "created_at": _raw_ts(meta),
    }

def raw_pod_to_dict(pod: dict) -> dict:
    meta, status, spec = pod.get("metadata", {}), pod.get("status", {}), pod.get("spec", {})
    return {"name": meta.get("name"), "namespace": meta.get("namespace"), "status": status.get("phase"), "node": spec.get("nodeName"), "pod_ip": status.get("podIP"), "host_ip": status.get("hostIP"), "labels": meta.get("labels", {}) or {}, "created_at": _raw_ts(meta)}

def raw_service_to_dict(svc: dict) -> dict:
    meta, spec = svc.get("metadata", {}), svc.get("spec", {})
    return {"name": meta.get("name"), "namespace": meta.get("namespace"), "type": spec.get("type"), "cluster_ip": spec.get("clusterIP"), "ports": [{"name": p.get("name"), "port": p.get("port"), "target_port": str(p.get("targetPort")), "node_port": p.get("nodePort"), "protocol": p.get("protocol")} for p in spec.get("ports", [])]}

# ---------- runtime helpers ----------
def normalize_image_name(image: str) -> str:
    if image.startswith(("docker.io/", "nvcr.io/", "quay.io/", "registry.k8s.io/", "localhost/")):
        return image
    if "/" not in image:
        return f"docker.io/library/{image}"
    return f"docker.io/{image}"

def get_instance_command(app_type: Optional[str], token: str) -> list[str]:
    app = (app_type or "terminal").lower()
    safe_token = token or uuid.uuid4().hex
    if app == "jupyter":
        return ["start-notebook.py", f"--ServerApp.token={safe_token}", "--ServerApp.allow_origin=*", "--ServerApp.ip=0.0.0.0", "--ServerApp.port=8888", "--ServerApp.root_dir=/home/jovyan/workspace"]
    if app == "vscode":
        return ["code-server", "--bind-addr", "0.0.0.0:8080", "--auth", "none", "/workspace"]
    return ["sleep", "infinity"]

def app_default_port(app_type: Optional[str]) -> int:
    app = (app_type or "terminal").lower()
    return 8888 if app == "jupyter" else 8080 if app == "vscode" else 22 if app == "ssh" else 8888

def infer_app_type_from_port(target_port: Optional[int], explicit_app_type: Optional[str] = None) -> str:
    if explicit_app_type: return explicit_app_type.lower()
    p = int(target_port or 0)
    return "jupyter" if p == 8888 else "vscode" if p == 8080 else "ssh" if p == 22 else "terminal"

def get_node_external_ip(node_name: str = NODE_NAME) -> str:
    try:
        node = k8s_raw_read_node(node_name)
        for addr in node.get("status", {}).get("addresses", []) or []:
            if addr.get("type") in ["ExternalIP", "InternalIP"]:
                return addr.get("address")
    except Exception as e:
        logger.warning(f"Failed to read node IP: {e}")
    return "192.168.10.226"

def build_launch_url(node_ip: str, node_port: int, app_type: Optional[str] = None, token: Optional[str] = None) -> str:
    app = (app_type or "terminal").lower()
    scheme = "ssh" if app == "ssh" else "http"
    base_url = f"{scheme}://{node_ip}:{node_port}"
    return f"{base_url}/lab?token={token or ''}" if app == "jupyter" else base_url

def calculate_resource_usage(node_name: str, resource_name: str) -> dict:
    node = k8s_raw_read_node(node_name)
    allocatable = node.get("status", {}).get("allocatable", {}) or {}
    try: capacity = int(allocatable.get(resource_name, "0"))
    except Exception: capacity = 0
    used = 0
    for pod in k8s_raw_list_pods(field_selector=f"spec.nodeName={node_name}"):
        if pod.get("status", {}).get("phase") in ["Succeeded", "Failed"]: continue
        for container in pod.get("spec", {}).get("containers", []) or []:
            limits = container.get("resources", {}).get("limits", {}) or {}
            if resource_name in limits:
                try: used += int(limits.get(resource_name, 0))
                except Exception: pass
    return {"capacity": capacity, "used": used, "free": max(capacity - used, 0)}

def provision_workspace_subpath(project_id: int, user_id: int, storage_id: Optional[int] = None) -> Tuple[str, str]:
    sub_path = f"project_{project_id}/user_{user_id}/storage_{storage_id}" if storage_id is not None else f"project_{project_id}/user_{user_id}/default_workspace"
    physical_path = os.path.join(STORAGE_BASE, sub_path)
    try:
        os.makedirs(physical_path, exist_ok=True)
        try:
            os.chown(physical_path, 1000, 100)
            os.chmod(physical_path, 0o775)
        except Exception as chown_err:
            logger.warning(f"chown/chmod 775 failed for {physical_path}: {chown_err}; falling back to chmod 777")
            os.chmod(physical_path, 0o777)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage provisioning failed: {e}")
    return sub_path, physical_path

def build_pod_manifest(db: Session, project: models.Project, user: models.User, plan: models.RentalPlan, instance_obj: models.Instance, safe_image: str, pod_name: str):
    app_type = (instance_obj.app_type or "terminal").lower()
    resource_name, requested_count = plan.k8s_resource_name, int(plan.resource_count)
    limits, requests = {resource_name: str(requested_count)}, {}
    if instance_obj.cpu_cores is not None: limits["cpu"] = requests["cpu"] = str(instance_obj.cpu_cores)
    if instance_obj.memory_gb is not None: limits["memory"] = requests["memory"] = f"{instance_obj.memory_gb}Gi"
    volumes, volume_mounts, pvc_name = [], [], None
    should_mount = instance_obj.storage_id is not None or app_type in ["jupyter", "vscode"]
    if should_mount:
        if instance_obj.storage_id is not None:
            storage = db.query(models.UserStorage).filter(models.UserStorage.id == instance_obj.storage_id).first()
            if not storage: raise HTTPException(status_code=404, detail="Storage allocation not found")
            if storage.user_id != user.id or storage.project_id != project.id:
                raise HTTPException(status_code=403, detail="Storage allocation does not belong to this user/project")
        sub_path, _ = provision_workspace_subpath(project.id, user.id, instance_obj.storage_id)
        pvc_name = PVC_NAME
        volumes.append(client.V1Volume(name="workspace-storage", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=pvc_name)))
        mount_target = "/home/jovyan/workspace" if app_type == "jupyter" else "/workspace"
        volume_mounts.append(client.V1VolumeMount(name="workspace-storage", mount_path=mount_target, sub_path=sub_path))
    if instance_obj.shm_gb is not None and instance_obj.shm_gb > 0:
        volumes.append(client.V1Volume(name="dshm", empty_dir=client.V1EmptyDirVolumeSource(medium="Memory", size_limit=f"{instance_obj.shm_gb}Gi")))
        volume_mounts.append(client.V1VolumeMount(name="dshm", mount_path="/dev/shm"))
    security_context = client.V1PodSecurityContext(run_as_user=1000, fs_group=100) if app_type == "jupyter" else None
    pod_manifest = client.V1Pod(
        metadata=client.V1ObjectMeta(name=pod_name, labels={"app":"gpu-tenant-instance", "pod_name":pod_name, "user_id":str(user.id), "project_id":str(project.id), "plan_id":str(plan.id), "billing":"true", "app_type":app_type}),
        spec=client.V1PodSpec(node_name=NODE_NAME, restart_policy="Never", security_context=security_context, volumes=volumes or None, containers=[client.V1Container(name="ai-workspace", image=safe_image, image_pull_policy="IfNotPresent", command=get_instance_command(app_type, pod_name), volume_mounts=volume_mounts or None, ports=[client.V1ContainerPort(container_port=app_default_port(app_type))], resources=client.V1ResourceRequirements(limits=limits, requests=requests or None))])
    )
    return pod_manifest, pvc_name
