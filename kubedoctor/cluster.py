"""Thin wrapper around the Kubernetes API.

Everything here returns plain dicts and strings on purpose. The output goes
straight into the graph state and the LLM prompt, and both are easier to work
with than the generated client models.
"""
from __future__ import annotations

from kubernetes import client, config
from kubernetes.client.rest import ApiException

_api = client.ApiClient()


def _plain(obj):
    return _api.sanitize_for_serialization(obj) if obj is not None else None


def pod_summary(pod) -> dict:
    containers = []
    for cs in pod.status.container_statuses or []:
        c = {
            "name": cs.name,
            "image": cs.image,
            "ready": bool(cs.ready),
            "restarts": cs.restart_count or 0,
        }
        st = cs.state
        if st and st.waiting:
            c.update(state="waiting", reason=st.waiting.reason, message=st.waiting.message)
        elif st and st.terminated:
            c.update(state="terminated", reason=st.terminated.reason,
                     exit_code=st.terminated.exit_code)
        else:
            c["state"] = "running"

        last = cs.last_state.terminated if cs.last_state else None
        if last:
            c["last_terminated"] = {"reason": last.reason, "exit_code": last.exit_code}
        containers.append(c)

    return {
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "node": pod.spec.node_name,
        "containers": containers,
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in pod.status.conditions or []
        ],
    }


def _event(e) -> dict:
    return {
        "object": f"{e.involved_object.kind}/{e.involved_object.name}",
        "type": e.type,
        "reason": e.reason,
        "message": (e.message or "").strip(),
        "count": e.count or 1,
    }


def _event_time(e):
    return e.last_timestamp or e.event_time or e.metadata.creation_timestamp


class Cluster:
    def __init__(self, context: str | None = None):
        try:
            config.load_kube_config(context=context)
        except config.ConfigException:
            config.load_incluster_config()
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.custom = client.CustomObjectsApi()
        self._pods: dict = {}

    # -- target ---------------------------------------------------------------

    def pods(self, kind: str, name: str, namespace: str) -> list[dict]:
        if kind == "pod":
            raw = [self.core.read_namespaced_pod(name, namespace)]
        elif kind == "deployment":
            dep = self.apps.read_namespaced_deployment(name, namespace)
            labels = dep.spec.selector.match_labels or {}
            selector = ",".join(f"{k}={v}" for k, v in labels.items())
            raw = self.core.list_namespaced_pod(namespace, label_selector=selector).items
        else:
            raise ValueError(f"unsupported kind '{kind}', use pod/<name> or deployment/<name>")

        self._pods = {p.metadata.name: p for p in raw}
        return [pod_summary(p) for p in raw]

    def deployment_status(self, name: str, namespace: str) -> dict:
        d = self.apps.read_namespaced_deployment(name, namespace)
        s = d.status
        return {
            "replicas": d.spec.replicas,
            "ready": s.ready_replicas or 0,
            "updated": s.updated_replicas or 0,
            "available": s.available_replicas or 0,
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                for c in s.conditions or []
            ],
        }

    # -- events ---------------------------------------------------------------

    def events(self, namespace: str, name: str, limit: int = 15) -> list[dict]:
        items = self.core.list_namespaced_event(
            namespace, field_selector=f"involvedObject.name={name}"
        ).items
        items.sort(key=_event_time)
        return [_event(e) for e in items[-limit:]]

    def replicaset_events(self, namespace: str, deployment: str) -> list[dict]:
        # Quota and admission errors land on the ReplicaSet, not the pods
        # (because the pods never get created).
        items = self.core.list_namespaced_event(
            namespace, field_selector="involvedObject.kind=ReplicaSet"
        ).items
        items = [e for e in items if e.involved_object.name.startswith(deployment + "-")]
        items.sort(key=_event_time)
        return [_event(e) for e in items[-10:]]

    # -- pod details ----------------------------------------------------------

    def _raw_pod(self, namespace: str, pod: str):
        if pod not in self._pods:
            self._pods[pod] = self.core.read_namespaced_pod(pod, namespace)
        return self._pods[pod]

    def container_names(self, namespace: str, pod: str) -> list[str]:
        return [c.name for c in self._raw_pod(namespace, pod).spec.containers]

    def container_specs(self, namespace: str, pod: str) -> list[dict]:
        out = []
        for c in self._raw_pod(namespace, pod).spec.containers:
            out.append({
                "name": c.name,
                "image": c.image,
                "command": c.command,
                "args": c.args,
                "resources": _plain(c.resources),
                "livenessProbe": _plain(c.liveness_probe),
                "readinessProbe": _plain(c.readiness_probe),
                "startupProbe": _plain(c.startup_probe),
                # names only - values can be secrets and have no business in a prompt
                "env": [e.name for e in c.env or []],
            })
        return out

    def logs(self, namespace: str, pod: str, container: str,
             previous: bool = False, tail: int = 60) -> str:
        try:
            text = self.core.read_namespaced_pod_log(
                pod, namespace, container=container, previous=previous, tail_lines=tail
            )
            return text or "<empty>"
        except ApiException as e:
            # previous=True on a container that never restarted gives a 400
            return f"<no logs: {e.status} {e.reason}>"

    def pod_metrics(self, namespace: str, pod: str) -> dict | None:
        try:
            m = self.custom.get_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods", pod
            )
            return {c["name"]: c["usage"] for c in m.get("containers", [])}
        except ApiException:
            return None  # metrics-server not installed, that's fine

    def node_capacity(self) -> list[dict]:
        return [
            {
                "name": n.metadata.name,
                "allocatable": n.status.allocatable,
                "taints": [f"{t.key}={t.value or ''}:{t.effect}" for t in n.spec.taints or []],
                "cordoned": bool(n.spec.unschedulable),
            }
            for n in self.core.list_node().items
        ]

    def missing_refs(self, namespace: str, pod: str) -> list[str]:
        """ConfigMaps/Secrets the pod references that don't exist."""
        p = self._raw_pod(namespace, pod)
        wanted = set()
        for c in p.spec.containers + (p.spec.init_containers or []):
            for e in c.env or []:
                src = e.value_from
                if src and src.config_map_key_ref:
                    wanted.add(("configmap", src.config_map_key_ref.name))
                if src and src.secret_key_ref:
                    wanted.add(("secret", src.secret_key_ref.name))
            for ef in c.env_from or []:
                if ef.config_map_ref:
                    wanted.add(("configmap", ef.config_map_ref.name))
                if ef.secret_ref:
                    wanted.add(("secret", ef.secret_ref.name))
        for v in p.spec.volumes or []:
            if v.config_map:
                wanted.add(("configmap", v.config_map.name))
            if v.secret:
                wanted.add(("secret", v.secret.secret_name))

        missing = []
        for kind, name in sorted(wanted):
            try:
                if kind == "configmap":
                    self.core.read_namespaced_config_map(name, namespace)
                else:
                    self.core.read_namespaced_secret(name, namespace)
            except ApiException as e:
                if e.status == 404:
                    missing.append(f"{kind}/{name}")
        return missing
