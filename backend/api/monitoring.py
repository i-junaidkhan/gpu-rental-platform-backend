import logging, csv, io
from fastapi import APIRouter, Depends, HTTPException, Response, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from datetime import datetime
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
    nodes = [raw_node_to_dict(n) for n in k8s_raw_list_nodes()]
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
def get_nodes(): return [raw_node_to_dict(n) for n in k8s_raw_list_nodes()]

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