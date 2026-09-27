"""Runs the whole graph against a fake cluster, no kubeconfig or LLM needed."""
from kubedoctor.graph import build_graph


class FakeCluster:
    def pods(self, kind, name, ns):
        return [{
            "name": "payments-api-5d8f-abc", "phase": "Running", "node": "kind-control-plane",
            "containers": [{"name": "app", "image": "busybox:1.36", "ready": False, "restarts": 5,
                            "state": "waiting", "reason": "CrashLoopBackOff",
                            "last_terminated": {"reason": "Error", "exit_code": 1}}],
            "conditions": [],
        }]

    def deployment_status(self, name, ns):
        return {"replicas": 1, "ready": 0, "updated": 1, "available": 0, "conditions": []}

    def events(self, ns, name):
        return [{"object": f"Pod/{name}", "type": "Warning", "reason": "BackOff",
                 "message": "Back-off restarting failed container", "count": 9}]

    def container_names(self, ns, pod):
        return ["app"]

    def logs(self, ns, pod, c, previous=False):
        return "payments-api starting\nFATAL: DATABASE_URL is not set, refusing to start\n"

    def container_specs(self, ns, pod):
        return [{"name": "app", "image": "busybox:1.36", "resources": {}, "env": []}]


def test_crashloop_end_to_end_without_llm():
    graph = build_graph(FakeCluster(), llm=None)
    out = graph.invoke({"kind": "deployment", "name": "payments-api",
                        "namespace": "kd-demo", "use_llm": False})

    assert out["symptoms"] == ["crashloop"]
    assert set(out["evidence"]) == {"previous_logs", "logs", "spec"}
    assert "DATABASE_URL" in out["diagnosis"]["root_cause"]
    assert out["diagnosis"]["source"] == "rules"
