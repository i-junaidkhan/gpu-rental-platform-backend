import logging, csv, io, urllib.parse, uuid, os
from fastapi import APIRouter, Depends, HTTPException, Response, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from database import get_db
from typing import Optional 
import models
from schemas import AllowedImageCreate, AllowedImageUpdate, AllowedImageResponse, AlertRuleCreate, AlertRuleUpdate, AlertRuleResponse
from services.kubernetes_svc import (
    k8s_raw_list_nodes, k8s_raw_read_node, k8s_raw_list_pods, k8s_raw_list_services,
    k8s_raw_request_text, raw_node_to_dict, raw_pod_to_dict, raw_service_to_dict, normalize_image_name, k8s_v1
)
from services.alert_notifier import send_slack_notification

router = APIRouter(tags=["Monitoring Images Billing Alerts"])
logger = logging.getLogger(__name__)

# ---------- Helper for node enrichment ----------
def _parse_k8s_quantity(qty: str) -> int:
    """Convert Kubernetes quantity like '32198428Ki' to integer bytes or cores."""
    if not qty:
        return 0
    qty = str(qty)
    if qty.isdigit():
        return int(qty)
    multipliers = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
    for suffix, mult in multipliers.items():
        if qty.endswith(suffix):
            try:
                return int(float(qty[:-len(suffix)]) * mult)
            except:
                return 0
    try:
        return int(float(qty))
    except:
        return 0

def _enrich_node_data(node: dict) -> dict:
    """Add pod count, conditions, node_info, uptime, resource_percent to raw node dict."""
    base = raw_node_to_dict(node)
    node_name = base.get("name")
    if not node_name:
        return base

    # 1. Pod count (excluding Succeeded/Failed)
    pods = k8s_raw_list_pods(field_selector=f"spec.nodeName={node_name}")
    base["pod_count"] = len([
        p for p in pods
        if p.get("status", {}).get("phase") not in ["Succeeded", "Failed"]
    ])

    # 2. Detailed conditions
    conditions = node.get("status", {}).get("conditions", []) or []
    base["conditions"] = {
        c.get("type"): {
            "status": c.get("status"),
            "reason": c.get("reason"),
            "message": c.get("message"),
            "lastTransition": c.get("lastTransitionTime")
        }
        for c in conditions
    }

    # 3. Node system info
    node_info = node.get("status", {}).get("nodeInfo", {}) or {}
    base["node_info"] = {
        "kubelet_version": node_info.get("kubeletVersion"),
        "kernel_version": node_info.get("kernelVersion"),
        "os_image": node_info.get("osImage"),
        "container_runtime": node_info.get("containerRuntimeVersion"),
        "architecture": node_info.get("architecture"),
    }

    # 4. Uptime (approximate)
    created = base.get("created_at")
    if created:
        try:
            if isinstance(created, str):
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                created_dt = created
            now = datetime.now(timezone.utc)
            uptime_seconds = (now - created_dt).total_seconds()
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            base["uptime"] = {
                "seconds": int(uptime_seconds),
                "human": f"{days}d {hours}h" if days > 0 else f"{hours}h"
            }
        except:
            base["uptime"] = None

    # 5. Resource percentages (allocatable / capacity)
    capacity = node.get("status", {}).get("capacity", {}) or {}
    allocatable = node.get("status", {}).get("allocatable", {}) or {}

    def calc_percent(cap_str, alloc_str):
        cap_val = _parse_k8s_quantity(cap_str)
        alloc_val = _parse_k8s_quantity(alloc_str)
        return round((alloc_val / cap_val) * 100, 1) if cap_val > 0 else None

    base["resource_percent"] = {
        "cpu": calc_percent(capacity.get("cpu"), allocatable.get("cpu")),
        "memory": calc_percent(capacity.get("memory"), allocatable.get("memory")),
        "pods": calc_percent(capacity.get("pods"), allocatable.get("pods")),
    }
    return base

# ---------- Images CRUD ----------
@router.get("/api/images", response_model=list[AllowedImageResponse])
def get_images(db: Session = Depends(get_db)):
    return db.query(models.AllowedImage).order_by(models.AllowedImage.id).all()

@router.post("/api/images", response_model=AllowedImageResponse)
def create_image(payload: AllowedImageCreate, db: Session = Depends(get_db)):
    data = payload.model_dump(); data["image_url"] = normalize_image_name(data["image_url"])
    row = models.AllowedImage(**data)
    try: db.add(row); db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Image creation failed")

@router.put("/api/images/{image_id}", response_model=AllowedImageResponse)
def update_image(image_id: int, payload: AllowedImageUpdate, db: Session = Depends(get_db)):
    row = db.query(models.AllowedImage).filter(models.AllowedImage.id == image_id).first()
    if not row: raise HTTPException(status_code=404, detail="Image not found")
    for k,v in payload.model_dump(exclude_unset=True).items(): setattr(row,k,v)
    db.commit(); db.refresh(row); return row

@router.delete("/api/images/{image_id}")
def delete_image(image_id: int, db: Session = Depends(get_db)):
    row = db.query(models.AllowedImage).filter(models.AllowedImage.id == image_id).first()
    if not row: raise HTTPException(status_code=404, detail="Image not found")
    db.delete(row); db.commit(); return {"message":"Image deleted", "id":image_id}

# ---------- Alerts CRUD ----------
@router.get("/api/alerts", response_model=list[AlertRuleResponse])
def get_alerts(db: Session = Depends(get_db)):
    return db.query(models.AlertRule).order_by(models.AlertRule.id).all()

@router.post("/api/alerts", response_model=AlertRuleResponse)
def create_alert(payload: AlertRuleCreate, db: Session = Depends(get_db)):
    row = models.AlertRule(**payload.model_dump())
    try: db.add(row); db.commit(); db.refresh(row); return row
    except SQLAlchemyError as e: db.rollback(); logger.error(e); raise HTTPException(status_code=400, detail="Alert creation failed")

@router.put("/api/alerts/{alert_id}", response_model=AlertRuleResponse)
def update_alert(alert_id: int, payload: AlertRuleUpdate, db: Session = Depends(get_db)):
    row = db.query(models.AlertRule).filter(models.AlertRule.id == alert_id).first()
    if not row: raise HTTPException(status_code=404, detail="Alert not found")
    for k,v in payload.model_dump(exclude_unset=True).items(): setattr(row,k,v)
    db.commit(); db.refresh(row); return row

@router.delete("/api/alerts/{alert_id}")
def delete_alert(alert_id: int, db: Session = Depends(get_db)):
    row = db.query(models.AlertRule).filter(models.AlertRule.id == alert_id).first()
    if not row: raise HTTPException(status_code=404, detail="Alert not found")
    db.delete(row); db.commit(); return {"message":"Alert deleted", "id":alert_id}

@router.put("/api/alerts/{alert_id}/toggle")
def toggle_alert(alert_id: int, db: Session = Depends(get_db)):
    rule = db.query(models.AlertRule).filter(models.AlertRule.id == alert_id).first()
    if not rule:
        raise HTTPException(404, "Alert rule not found")
    rule.enabled = not rule.enabled
    db.commit()
    return {"id": alert_id, "enabled": rule.enabled}

# ---------- Alert status with Slack delivery ----------
@router.get("/api/alerts/status")
def alerts_status(db: Session = Depends(get_db)):
    nodes = [_enrich_node_data(n) for n in k8s_raw_list_nodes()]
    gpu_rows = monitoring_gpus()
    unhealthy_pods = [raw_pod_to_dict(p) for p in k8s_raw_list_pods(namespace="gpu-rental-system") if p.get("status", {}).get("phase") not in ["Running", "Succeeded"]]
    rules = db.query(models.AlertRule).filter(models.AlertRule.enabled == True).all()
    for rule in rules:
        if rule.metric == "gpu_advertised":
            problem_gpus = [g for g in gpu_rows if g.get("status") == "gpu_present_not_advertised"]
            if problem_gpus and rule.action in ["slack", "webhook"]:
                msg = f"Alert '{rule.name}': {len(problem_gpus)} GPUs not advertised on nodes {[g['node_name'] for g in problem_gpus]}"
                if rule.action == "slack":
                    send_slack_notification(msg)
        elif rule.metric == "node_ready":
            not_ready = [n for n in nodes if not n.get("ready")]
            if not_ready and rule.action in ["slack", "webhook"]:
                msg = f"Alert '{rule.name}': Nodes not ready: {[n['name'] for n in not_ready]}"
                if rule.action == "slack":
                    send_slack_notification(msg)
    return {"enabled_rules": len(rules), "not_ready_nodes": [n for n in nodes if not n.get("ready")], "gpu_rows": gpu_rows, "unhealthy_pods": unhealthy_pods, "delivery": "Slack webhook enabled"}

# ---------- Hardware monitoring ----------
@router.get("/api/hardware/status")
def hardware_status():
    return {"nodes": monitoring_nodes(), "gpus": monitoring_gpus(), "unhealthy_pods": [raw_pod_to_dict(p) for p in k8s_raw_list_pods(namespace="gpu-rental-system") if p.get("status", {}).get("phase") not in ["Running", "Succeeded"]]}

@router.get("/api/monitoring/pods")
def monitoring_pods(db: Session = Depends(get_db)):
    return [{"instance_id":i.id, "pod_name":i.pod_name, "namespace":i.namespace, "project_id":i.project_id, "user_id":i.user_id, "plan_id":i.plan_id, "status":i.status.value if hasattr(i.status,'value') else str(i.status), "accumulated_cost":i.accumulated_cost or 0.0} for i in db.query(models.Instance).all()]

@router.get("/api/monitoring/nodes")
def monitoring_nodes():
    result = []
    for node in k8s_raw_list_nodes():
        nd = raw_node_to_dict(node)
        result.append({"name":nd.get("name"), "ready":nd.get("ready"), "cpu":nd.get("capacity",{}).get("cpu"), "memory":nd.get("capacity",{}).get("memory"), "gpu":nd.get("capacity",{}).get("nvidia.com/gpu", "0"), "internal_ip":nd.get("internal_ip"), "status":nd.get("status"), "labels":nd.get("labels", {})})
    return result

@router.get("/api/monitoring/gpus")
def monitoring_gpus():
    rows=[]
    for node in k8s_raw_list_nodes():
        meta, status = node.get("metadata",{}), node.get("status",{})
        labels, capacity, allocatable = meta.get("labels",{}) or {}, status.get("capacity",{}) or {}, status.get("allocatable",{}) or {}
        found=False
        for key in sorted(set(list(capacity.keys())+list(allocatable.keys()))):
            if key.startswith("nvidia.com/"):
                found=True
                try: cap=int(capacity.get(key,"0"))
                except Exception: cap=0
                try: alloc=int(allocatable.get(key,"0"))
                except Exception: alloc=0
                rows.append({"node_name":meta.get("name"), "resource_name":key, "capacity":cap, "allocatable":alloc, "product":labels.get("nvidia.com/gpu.product") or labels.get(f"{key}.product"), "mig_strategy":labels.get("nvidia.com/mig.strategy"), "status":"available" if alloc>0 else "gpu_present_not_advertised"})
        if not found and (labels.get("feature.node.kubernetes.io/pci-10de.present") == "true" or labels.get("nvidia.com/gpu.present") == "true"):
            rows.append({"node_name":meta.get("name"), "resource_name":"nvidia.com/gpu", "capacity":0, "allocatable":0, "product":labels.get("nvidia.com/gpu.product"), "mig_strategy":labels.get("nvidia.com/mig.strategy"), "status":"gpu_present_not_advertised"})
    return rows

# ---------- Billing ----------
@router.get("/api/billing/usage/raw")
def billing_usage_raw(db: Session = Depends(get_db)):
    rows=[]
    for i in db.query(models.Instance).all():
        plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == i.plan_id).first()
        rows.append({"instance_id":i.id, "pod_name":i.pod_name, "user_id":i.user_id, "project_id":i.project_id, "plan_id":i.plan_id, "status":i.status.value if hasattr(i.status,'value') else str(i.status), "price_per_hour":float(plan.price_per_hour) if plan else 0.0, "accumulated_cost":float(i.accumulated_cost or 0.0)})
    return rows

@router.get("/api/billing/usage/summary")
def billing_usage_summary(period: str = "daily", db: Session = Depends(get_db)):
    rows = billing_usage_raw(db); total=sum(float(r.get("accumulated_cost") or 0) for r in rows)
    grouped={}
    for r in rows: grouped.setdefault(r.get("project_id"), []).append(r)
    return {"period":period, "total_instances":len(rows), "active_instances":sum(1 for r in rows if str(r.get("status")).lower()=="running"), "total_accumulated_cost":total, "by_project":[{"project_id":pid, "instances":len(items), "active_instances":sum(1 for r in items if str(r.get("status")).lower()=="running"), "accumulated_cost":sum(float(r.get("accumulated_cost") or 0) for r in items)} for pid,items in grouped.items()]}

@router.post("/api/billing/calculate")
def billing_calculate(db: Session = Depends(get_db)):
    from datetime import datetime
    now = datetime.utcnow()
    running_instances = db.query(models.Instance).filter(models.Instance.status == models.InstanceStatusEnum.RUNNING).all()
    total_added = 0.0
    for inst in running_instances:
        last_event = db.query(models.BillingEvent).filter(
            models.BillingEvent.instance_id == inst.id,
            models.BillingEvent.event_type == "usage"
        ).order_by(models.BillingEvent.timestamp.desc()).first()
        last_time = last_event.timestamp if last_event else inst.created_at
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=None)
            now_naive = now.replace(tzinfo=None)
        else:
            now_naive = now
        hours_elapsed = (now_naive - last_time).total_seconds() / 3600.0
        if hours_elapsed <= 0:
            continue
        plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == inst.plan_id).first()
        if not plan:
            continue
        cost = round(plan.price_per_hour * hours_elapsed, 6)
        inst.accumulated_cost = float(inst.accumulated_cost or 0.0) + cost
        event = models.BillingEvent(
            project_id=inst.project_id,
            user_id=inst.user_id,
            instance_id=inst.id,
            amount=cost,
            event_type="usage"
        )
        db.add(event)
        total_added += cost
    db.commit()
    return {"message": "Billing calculated", "instances_processed": len(running_instances), "total_cost_added": round(total_added, 6)}

@router.get("/api/billing/export")
def billing_export(
    start_date: Optional[str] = Query(None, description="ISO datetime start filter"),
    end_date: Optional[str] = Query(None, description="ISO datetime end filter"),
    project_id: Optional[int] = Query(None, description="Filter by project ID"),
    db: Session = Depends(get_db)
):
    query = db.query(models.Instance)
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        except:
            start_dt = None
        if start_dt:
            query = query.filter(models.Instance.created_at >= start_dt)
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        except:
            end_dt = None
        if end_dt:
            query = query.filter(models.Instance.created_at <= end_dt)
    if project_id:
        query = query.filter(models.Instance.project_id == project_id)
    instances = query.order_by(models.Instance.id).all()
    rows = []
    for i in instances:
        plan = db.query(models.RentalPlan).filter(models.RentalPlan.id == i.plan_id).first()
        user = db.query(models.User).filter(models.User.id == i.user_id).first()
        project = db.query(models.Project).filter(models.Project.id == i.project_id).first()
        rows.append({
            "instance_id": i.id,
            "pod_name": i.pod_name,
            "display_name": i.display_name or "",
            "project_id": i.project_id,
            "project_name": project.name if project else "",
            "user_id": i.user_id,
            "username": user.username if user else "",
            "plan_id": i.plan_id,
            "plan_name": plan.name if plan else "",
            "status": i.status.value,
            "price_per_hour": plan.price_per_hour if plan else 0.0,
            "accumulated_cost": i.accumulated_cost or 0.0,
            "created_at": i.created_at.isoformat() if i.created_at else "",
            "app_type": i.app_type or "",
            "cpu_cores": i.cpu_cores or "",
            "memory_gb": i.memory_gb or "",
            "shm_gb": i.shm_gb or ""
        })
    if not rows:
        # Return empty CSV with headers
        rows = [{"instance_id":"","pod_name":"","display_name":"","project_id":"","project_name":"","user_id":"","username":"","plan_id":"","plan_name":"","status":"","price_per_hour":"","accumulated_cost":"","created_at":"","app_type":"","cpu_cores":"","memory_gb":"","shm_gb":""}]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)
    return Response(content=out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=billing_export.csv"})

# ---------- Kubernetes resource endpoints ----------
@router.get("/api/nodes")
def get_nodes():
    return [_enrich_node_data(n) for n in k8s_raw_list_nodes()]

@router.get("/api/namespaces")
def get_namespaces():
    try:
        ns_list = k8s_v1.list_namespace()
        return [{"name": ns.metadata.name, "status": ns.status.phase, "created_at": ns.metadata.creation_timestamp} for ns in ns_list.items]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"K8s error: {e}")

@router.get("/api/pods")
def get_pods(namespace: str | None = None):
    try:
        if namespace:
            pod_list = k8s_v1.list_namespaced_pod(namespace)
        else:
            pod_list = k8s_v1.list_pod_for_all_namespaces()
        return [{"name": p.metadata.name, "namespace": p.metadata.namespace, "status": p.status.phase} for p in pod_list.items]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"K8s error: {e}")

@router.get("/api/services")
def get_services(namespace: str | None = None):
    try:
        if namespace:
            svc_list = k8s_v1.list_namespaced_service(namespace)
        else:
            svc_list = k8s_v1.list_service_for_all_namespaces()
        return [{"name": s.metadata.name, "namespace": s.metadata.namespace, "type": s.spec.type} for s in svc_list.items]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"K8s error: {e}")

@router.get("/api/gpu-inventory")
def get_gpu_inventory():
    node = k8s_raw_read_node("g01"); status=node.get("status",{}); allocatable=status.get("allocatable",{}) or {}; capacity=status.get("capacity",{}) or {}
    pods = k8s_raw_list_pods(namespace="gpu-rental-system"); used={"nvidia.com/gpu":0, "nvidia.com/mig-1g.10gb":0}
    for pod in pods:
        for c in pod.get("spec",{}).get("containers",[]) or []:
            limits=c.get("resources",{}).get("limits",{}) or {}
            for k in used:
                if k in limits:
                    try: used[k]+=int(limits[k])
                    except Exception: pass
    resources=[]
    for idx,(rn,name,typ) in enumerate([("nvidia.com/gpu","A100 Shared Slot","shared"),("nvidia.com/mig-1g.10gb","A100 MIG 1g.10gb","mig")], start=1):
        cap=int(capacity.get(rn,0)); alloc=int(allocatable.get(rn,0)); available=max(cap-used[rn],0)
        resources.append({"id":idx,"node_name":"g01","name":name,"resource_name":rn,"capacity":cap,"used":used[rn],"available":available,"allocatable":alloc,"product":"NVIDIA-A100-80GB-PCIe","type":typ,"status":"available" if available>0 else "unavailable"})
    return resources

@router.get("/api/k8s/node-resources/{node_name}")
def get_node_resources(node_name: str):
    node=k8s_raw_read_node(node_name); status=node.get("status",{}); cap=status.get("capacity",{}) or {}; alloc=status.get("allocatable",{}) or {}; labels=node.get("metadata",{}).get("labels",{}) or {}
    return {"node":node_name, "gpu_resources":{k:{"capacity":cap.get(k,"0"), "allocatable":alloc.get(k,"0")} for k in sorted(set(list(cap.keys())+list(alloc.keys()))) if k.startswith("nvidia.com/")}, "labels":labels}

@router.get("/api/k8s/all-pods")
def get_all_pods(): return [raw_pod_to_dict(p) for p in k8s_raw_list_pods()]
@router.get("/api/k8s/gpu-pods")
def get_gpu_pods():
    res=[]
    for p in k8s_raw_list_pods():
        uses = (p.get("metadata",{}).get("labels",{}) or {}).get("app") == "gpu-tenant-instance"
        for c in p.get("spec",{}).get("containers",[]) or []:
            if any(k.startswith("nvidia.com/") for k in (c.get("resources",{}).get("limits",{}) or {}).keys()): uses=True
        if uses: res.append(raw_pod_to_dict(p))
    return res
@router.get("/api/k8s/node-summary/{node_name}")
def get_node_summary(node_name: str): return {"node":node_name, "resources":get_node_resources(node_name).get("gpu_resources",{}), "pod_count":len(k8s_raw_list_pods(field_selector=f"spec.nodeName={node_name}"))}
@router.get("/api/k8s/pod-status/{namespace}/{pod_name}")
def get_pod_status(namespace: str, pod_name: str): return raw_pod_to_dict(__import__('services.kubernetes_svc', fromlist=['k8s_raw_read_pod']).k8s_raw_read_pod(namespace,pod_name))
@router.get("/api/k8s/pod-exec/{namespace}/{pod_name}")
def pod_exec_placeholder(namespace: str, pod_name: str): return {"namespace":namespace, "pod_name":pod_name, "output":"Exec endpoint placeholder. GPU status exec is not enabled in MVP."}

# ========== NEW: Node Management Endpoints (0순위) ==========

@router.get("/api/nodes/{node_name}/metrics")
def get_node_metrics(node_name: str):
    """Real-time CPU/Memory usage % via Kubernetes Metrics API."""
    try:
        metrics_path = f"/apis/metrics.k8s.io/v1beta1/nodes/{urllib.parse.quote(node_name)}"
        metrics = __import__('services.kubernetes_svc', fromlist=['k8s_raw_request']).k8s_raw_request("GET", metrics_path)
        usage = metrics.get("usage", {})
        node = k8s_raw_read_node(node_name)
        capacity = node.get("status", {}).get("capacity", {}) or {}

        def parse_qty(val: str) -> float:
            if not val:
                return 0
            val = str(val)
            if val.endswith("Ki"):
                return float(val[:-2]) / 1024
            if val.endswith("Mi"):
                return float(val[:-2])
            if val.endswith("Gi"):
                return float(val[:-2]) * 1024
            if val.endswith("m"):
                return float(val[:-1]) / 1000
            return float(val)

        cpu_usage = parse_qty(usage.get("cpu", "0"))
        cpu_cap = parse_qty(capacity.get("cpu", "1"))
        mem_usage = parse_qty(usage.get("memory", "0"))
        mem_cap = parse_qty(capacity.get("memory", "1"))

        return {
            "cpu_usage_percent": round((cpu_usage / cpu_cap) * 100, 1) if cpu_cap else 0,
            "memory_usage_percent": round((mem_usage / mem_cap) * 100, 1) if mem_cap else 0,
            "timestamp": metrics.get("timestamp")
        }
    except Exception:
        return {"cpu_usage_percent": None, "memory_usage_percent": None, "fallback": True}

@router.get("/api/nodes/{node_name}/pods")
def get_node_pods(node_name: str, db: Session = Depends(get_db)):
    pods = k8s_raw_list_pods(field_selector=f"spec.nodeName={node_name}")
    result = []
    for pod in pods:
        instance = db.query(models.Instance).filter(models.Instance.pod_name == pod.get("metadata", {}).get("name")).first()
        result.append({
            "name": pod.get("metadata", {}).get("name"),
            "namespace": pod.get("metadata", {}).get("namespace"),
            "status": pod.get("status", {}).get("phase"),
            "instance_id": instance.id if instance else None,
            "cpu_request": pod.get("spec", {}).get("containers", [{}])[0].get("resources", {}).get("requests", {}).get("cpu"),
            "memory_request": pod.get("spec", {}).get("containers", [{}])[0].get("resources", {}).get("requests", {}).get("memory"),
            "gpu_request": pod.get("spec", {}).get("containers", [{}])[0].get("resources", {}).get("limits", {}).get("nvidia.com/gpu"),
            "created_at": pod.get("metadata", {}).get("creationTimestamp")
        })
    return result

@router.get("/api/nodes/{node_name}/events")
def get_node_events(node_name: str):
    try:
        events_path = f"/api/v1/events?fieldSelector=involvedObject.name={urllib.parse.quote(node_name)},involvedObject.kind=Node"
        data = __import__('services.kubernetes_svc', fromlist=['k8s_raw_request']).k8s_raw_request("GET", events_path)
        items = data.get("items", [])
        return [
            {
                "type": e.get("type"),
                "reason": e.get("reason"),
                "message": e.get("message"),
                "count": e.get("count"),
                "last_seen": e.get("lastTimestamp")
            } for e in items[-20:]
        ]
    except Exception:
        return []

@router.post("/api/nodes/{node_name}/cordon")
def cordon_node(node_name: str):
    try:
        k8s_v1.patch_node(name=node_name, body={"spec": {"unschedulable": True}})
        return {"message": f"Node {node_name} cordoned", "unschedulable": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/api/nodes/{node_name}/uncordon")
def uncordon_node(node_name: str):
    try:
        k8s_v1.patch_node(name=node_name, body={"spec": {"unschedulable": False}})
        return {"message": f"Node {node_name} uncordoned", "unschedulable": False}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/api/nodes/{node_name}/drain")
def drain_node(node_name: str, force: bool = False, ignore_daemonsets: bool = True):
    try:
        pods = k8s_raw_list_pods(field_selector=f"spec.nodeName={node_name}")
        evicted = []
        for pod in pods:
            meta = pod.get("metadata", {})
            name = meta.get("name")
            namespace = meta.get("namespace", "default")
            if ignore_daemonsets and any(owner.get("kind") == "DaemonSet" for owner in meta.get("ownerReferences", [])):
                continue
            try:
                eviction = {
                    "apiVersion": "policy/v1",
                    "kind": "Eviction",
                    "metadata": {"name": name, "namespace": namespace}
                }
                __import__('services.kubernetes_svc', fromlist=['k8s_raw_request']).k8s_raw_request("POST", f"/api/v1/namespaces/{namespace}/pods/{name}/eviction", body=eviction)
                evicted.append(name)
            except Exception:
                pass
        k8s_v1.patch_node(name=node_name, body={"spec": {"unschedulable": True}})
        return {"message": f"Drained {len(evicted)} pods", "evicted_pods": evicted}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.delete("/api/nodes/{node_name}")
def delete_node(node_name: str, force: bool = False):
    try:
        if not force:
            pods = k8s_raw_list_pods(field_selector=f"spec.nodeName={node_name}")
            for pod in pods:
                meta = pod.get("metadata", {})
                if not any(owner.get("kind") == "DaemonSet" for owner in meta.get("ownerReferences", [])):
                    try:
                        k8s_v1.delete_namespaced_pod(meta.get("name"), meta.get("namespace", "default"), grace_period_seconds=30)
                    except:
                        pass
        k8s_v1.delete_node(name=node_name)
        return {"message": f"Node {node_name} deleted"}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/api/nodes/register")
def register_node(payload: dict):
    try:
        master_ip = os.getenv("KUBERNETES_SERVICE_HOST", "192.168.10.226")
        master_port = os.getenv("KUBERNETES_SERVICE_PORT", "6443")
        token = uuid.uuid4().hex[:6] + "." + uuid.uuid4().hex[:16]
        join_command = f"kubeadm join {master_ip}:{master_port} --token {token} --discovery-token-unsafe-skip-ca-verification"
        if payload.get("is_gpu_node"):
            join_command += " --node-labels=nvidia.com/gpu.present=true"
        return {
            "join_command": join_command,
            "token": token,
            "master_endpoint": f"{master_ip}:{master_port}",
            "instructions": [
                "1. SSH into new node",
                "2. Install kubeadm, kubelet, kubectl, container runtime",
                f"3. Run: {join_command}",
                "4. For GPU nodes: install NVIDIA drivers + device plugin"
            ]
        }
    except Exception as e:
        raise HTTPException(500, str(e))