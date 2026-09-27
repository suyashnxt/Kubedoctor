from kubedoctor.rules import detect_symptoms, plan_checks


def pod(containers, phase="Running", conditions=None):
    return {"name": "p-1", "phase": phase, "node": "n1",
            "containers": containers, "conditions": conditions or []}


def container(**kw):
    base = {"name": "app", "image": "x:1", "ready": False, "restarts": 0, "state": "running"}
    base.update(kw)
    return base


def test_crashloop():
    p = pod([container(state="waiting", reason="CrashLoopBackOff", restarts=4,
                       last_terminated={"reason": "Error", "exit_code": 1})])
    assert detect_symptoms([p], []) == ["crashloop"]


def test_oom_wins_over_crashloop():
    p = pod([container(state="waiting", reason="CrashLoopBackOff", restarts=3,
                       last_terminated={"reason": "OOMKilled", "exit_code": 137})])
    assert detect_symptoms([p], []) == ["oomkilled"]


def test_image_pull():
    p = pod([container(state="waiting", reason="ImagePullBackOff")], phase="Pending")
    assert detect_symptoms([p], []) == ["image_pull"]


def test_probe_failure_replaces_not_ready():
    p = pod([container(ready=False)])
    ev = [{"reason": "Unhealthy", "message": "Readiness probe failed: HTTP probe failed with statuscode: 404"}]
    assert detect_symptoms([p], ev) == ["probe_failure"]


def test_unschedulable():
    p = pod([], phase="Pending",
            conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": ""}])
    assert detect_symptoms([p], []) == ["unschedulable"]


def test_healthy_pod_has_no_symptoms():
    assert detect_symptoms([pod([container(ready=True)])], []) == []


def test_plan_dedupes_and_keeps_order():
    assert plan_checks(["crashloop", "probe_failure"]) == ["previous_logs", "logs", "spec"]
