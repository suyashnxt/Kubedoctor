"""Deterministic checks that run before (and sometimes instead of) the LLM.

The rules decide *what to look at*. The LLM, if enabled, decides *what it
means*. Keeping the first half deterministic makes the agent predictable and
cheap: we only pull logs or node info when a symptom says it's worth it.
"""
from __future__ import annotations

import re

IMAGE_PULL = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}
CONFIG = {"CreateContainerConfigError"}
CRASH = {"CrashLoopBackOff", "Error", "RunContainerError", "CreateContainerError"}

# order matters: the first symptom found is treated as the primary one
PRIORITY = [
    "image_pull", "config_error", "unschedulable", "oomkilled",
    "crashloop", "probe_failure", "volume", "not_ready", "no_pods",
]

CHECKS = {
    "crashloop": ["previous_logs", "logs", "spec"],
    "oomkilled": ["spec", "metrics"],
    "image_pull": ["spec"],
    "config_error": ["config_refs", "spec"],
    "probe_failure": ["spec", "logs"],
    "not_ready": ["spec", "logs"],
    "unschedulable": ["spec", "nodes"],
    "volume": ["spec"],
    "no_pods": ["replicaset_events"],
}


def is_healthy(pod: dict) -> bool:
    if pod["phase"] == "Succeeded":
        return True
    return (
        pod["phase"] == "Running"
        and bool(pod["containers"])
        and all(c["ready"] for c in pod["containers"])
    )


def detect_symptoms(pods: list[dict], events: list[dict]) -> list[str]:
    found: set[str] = set()

    for pod in pods:
        for c in pod["containers"]:
            reason = c.get("reason") or ""
            last = (c.get("last_terminated") or {}).get("reason")

            # An OOMKilled container also shows CrashLoopBackOff once it has
            # restarted a few times. OOM is the more useful label.
            if reason == "OOMKilled" or last == "OOMKilled":
                found.add("oomkilled")
            elif reason in IMAGE_PULL:
                found.add("image_pull")
            elif reason in CONFIG:
                found.add("config_error")
            elif reason in CRASH or (c["restarts"] > 0 and last not in (None, "Completed")):
                found.add("crashloop")
            elif c["state"] == "running" and not c["ready"]:
                found.add("not_ready")

        for cond in pod["conditions"]:
            if cond["type"] == "PodScheduled" and cond["status"] == "False":
                found.add("unschedulable")

    for ev in events:
        if ev["reason"] == "Unhealthy":
            found.add("probe_failure")
        elif ev["reason"] == "FailedScheduling":
            found.add("unschedulable")
        elif ev["reason"] in ("FailedMount", "FailedAttachVolume"):
            found.add("volume")

    if "probe_failure" in found:
        found.discard("not_ready")  # same thing, but probe_failure tells us why

    return [s for s in PRIORITY if s in found]


def plan_checks(symptoms: list[str]) -> list[str]:
    plan: list[str] = []
    for s in symptoms:
        for check in CHECKS.get(s, []):
            if check not in plan:
                plan.append(check)
    return plan


# ---------------------------------------------------------------------------
# Rule-based diagnosis, used with --no-llm or when the LLM call fails
# ---------------------------------------------------------------------------

ERROR_LINE = re.compile(
    r"error|exception|fatal|panic|traceback|refused|denied|not found|no such|missing|not set",
    re.I,
)


def _error_lines(text: str, n: int = 3) -> list[str]:
    return [l.strip() for l in text.splitlines() if ERROR_LINE.search(l)][-n:]


def _last_event(events: list[dict], *reasons: str) -> dict | None:
    for ev in reversed(events):
        if ev["reason"] in reasons:
            return ev
    return None


def _first_container(state: dict, pod: str) -> dict:
    for p in state["pods"]:
        if p["name"] == pod and p["containers"]:
            return p["containers"][0]
    return {}


def _spec(state: dict, pod: str) -> list[dict]:
    return state["evidence"].get("spec", {}).get(pod, [])


def fallback_diagnosis(state: dict) -> dict:
    symptoms = state["symptoms"]
    ns = state["namespace"]
    pod = state["focus"][0] if state["focus"] else state["name"]
    events = state["events"]
    ev_dump = state["evidence"]
    primary = symptoms[0]
    describe = f"kubectl -n {ns} describe pod {pod}"

    if primary == "image_pull":
        c = _first_container(state, pod)
        ev = _last_event(events, "Failed")
        return _d(
            f"Image {c.get('image', '?')} can't be pulled",
            ev["message"] if ev else c.get("message") or "Image pull is failing.",
            "high" if ev else "medium",
            [ev["message"]] if ev else [],
            ["Check the image name and tag actually exist in the registry",
             "If the registry is private, add imagePullSecrets to the pod spec"],
            [describe],
        )

    if primary == "config_error":
        missing = []
        for refs in ev_dump.get("config_refs", {}).values():
            missing += refs
        if missing:
            return _d(
                "Pod references config that doesn't exist",
                f"Missing: {', '.join(missing)}. The kubelet won't start the container until it exists.",
                "high", missing,
                [f"Create {m} in namespace {ns}, or fix the name in the manifest" for m in missing],
                [f"kubectl -n {ns} get configmap,secret"],
            )
        c = _first_container(state, pod)
        return _d("Container config error", c.get("message") or "CreateContainerConfigError",
                  "medium", [c.get("message", "")], ["Check env/envFrom and volume references"], [describe])

    if primary == "unschedulable":
        ev = _last_event(events, "FailedScheduling")
        spec = _spec(state, pod)
        requests = spec[0]["resources"].get("requests") if spec and spec[0].get("resources") else None
        return _d(
            "Scheduler can't find a node for the pod",
            ev["message"] if ev else "Pod is stuck in Pending.",
            "high" if ev else "low",
            [x for x in [ev and ev["message"], requests and f"requests: {requests}"] if x],
            ["Lower the CPU/memory requests if they're larger than any node can offer",
             "Otherwise add capacity, or check taints / nodeSelector / affinity"],
            [describe, "kubectl describe nodes | grep -A5 Allocated"],
        )

    if primary == "oomkilled":
        spec = _spec(state, pod)
        limits = spec[0]["resources"].get("limits") if spec and spec[0].get("resources") else None
        usage = (ev_dump.get("metrics") or {}).get(pod)
        return _d(
            "Container is being OOMKilled",
            f"The container exceeds its memory limit ({(limits or {}).get('memory', 'not set')}) and the kernel kills it.",
            "high",
            [x for x in [f"limits: {limits}" if limits else None, f"current usage: {usage}" if usage else None] if x],
            ["Raise resources.limits.memory if the app genuinely needs more",
             "If usage keeps growing until it dies, look for a memory leak instead"],
            [describe, f"kubectl -n {ns} top pod {pod}"],
        )

    if primary == "crashloop":
        logs = ev_dump.get("previous_logs", {}).get(pod, {}) or ev_dump.get("logs", {}).get(pod, {})
        lines = []
        for text in logs.values():
            lines += _error_lines(text)
        c = _first_container(state, pod)
        code = (c.get("last_terminated") or {}).get("exit_code", c.get("exit_code"))
        return _d(
            "Container keeps crashing on startup",
            (f"Process exits with code {code}. " if code is not None else "")
            + (f"Last error in logs: {lines[-1]}" if lines else "Nothing obvious in the logs, check the command/args."),
            "medium" if lines else "low",
            lines,
            ["Fix the error shown in the logs (often a missing env var or bad config)",
             "Run the image locally with the same command to reproduce"],
            [f"kubectl -n {ns} logs {pod} --previous", describe],
        )

    if primary == "probe_failure":
        ev = _last_event(events, "Unhealthy")
        spec = _spec(state, pod)
        probe = (spec[0].get("readinessProbe") or spec[0].get("livenessProbe")) if spec else None
        return _d(
            "Health probe is failing",
            ev["message"] if ev else "Probe failing.",
            "medium",
            [x for x in [ev and ev["message"], probe and f"probe: {probe}"] if x],
            ["Check the probe path and port match what the app actually serves",
             "If the app is just slow to start, add a startupProbe or raise initialDelaySeconds"],
            [describe],
        )

    return _d(
        f"Detected: {', '.join(symptoms)}",
        "No specific rule for this, see events below.",
        "low",
        [f"{e['reason']}: {e['message']}" for e in events[-5:]],
        ["Re-run with the LLM enabled for a deeper read"],
        [describe],
    )


def _d(summary, root_cause, confidence, evidence, fix, commands) -> dict:
    return {
        "summary": summary,
        "root_cause": root_cause,
        "confidence": confidence,
        "evidence": evidence,
        "fix": fix,
        "commands": commands,
    }
