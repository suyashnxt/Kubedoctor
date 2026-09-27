"""The investigation graph.

    collect -> triage --(healthy)--> healthy -> END
                  |
                  +--> investigate <--+  (loops once per planned check)
                          |           |
                          +-----------+
                          |
                          v
                       diagnose -> END

`triage` looks at pod status + events and decides which checks are worth
running. `investigate` runs them one at a time, so you can watch the agent
work through its plan in the CLI output.
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from . import rules
from .prompts import Diagnosis, build_messages

log = logging.getLogger(__name__)

MAX_FOCUS_PODS = 2  # logs from 20 identical crashing replicas don't help anyone


class State(TypedDict, total=False):
    kind: str
    name: str
    namespace: str
    use_llm: bool

    deployment: dict | None
    pods: list[dict]
    events: list[dict]
    focus: list[str]          # pods we dig into

    symptoms: list[str]
    pending: list[str]        # checks still to run
    evidence: dict[str, Any]  # check name -> result
    diagnosis: dict


def build_graph(cluster, llm=None):

    def collect(state: State) -> State:
        ns, name, kind = state["namespace"], state["name"], state["kind"]
        pods = cluster.pods(kind, name, ns)

        unhealthy = [p["name"] for p in pods if not rules.is_healthy(p)]
        focus = unhealthy[:MAX_FOCUS_PODS] or [p["name"] for p in pods[:1]]

        events = cluster.events(ns, name)
        for pod in focus:
            if pod != name:
                events += cluster.events(ns, pod)

        return {
            "pods": pods,
            "events": events,
            "focus": focus,
            "deployment": cluster.deployment_status(name, ns) if kind == "deployment" else None,
        }

    def triage(state: State) -> State:
        if state["kind"] == "deployment" and not state["pods"]:
            symptoms = ["no_pods"]
        else:
            symptoms = rules.detect_symptoms(state["pods"], state["events"])
        return {"symptoms": symptoms, "pending": rules.plan_checks(symptoms), "evidence": {}}

    def investigate(state: State) -> State:
        check, *rest = state["pending"]
        evidence = dict(state["evidence"])
        evidence[check] = run_check(check, state)
        return {"pending": rest, "evidence": evidence}

    def run_check(check: str, state: State):
        ns = state["namespace"]
        if check in ("logs", "previous_logs"):
            prev = check == "previous_logs"
            return {
                pod: {c: cluster.logs(ns, pod, c, previous=prev) for c in cluster.container_names(ns, pod)}
                for pod in state["focus"]
            }
        if check == "spec":
            return {pod: cluster.container_specs(ns, pod) for pod in state["focus"]}
        if check == "metrics":
            return {pod: cluster.pod_metrics(ns, pod) for pod in state["focus"]}
        if check == "config_refs":
            return {pod: cluster.missing_refs(ns, pod) for pod in state["focus"]}
        if check == "nodes":
            return cluster.node_capacity()
        if check == "replicaset_events":
            return cluster.replicaset_events(ns, state["name"])
        raise ValueError(f"unknown check {check}")

    def diagnose(state: State) -> State:
        if state.get("use_llm") and llm is not None:
            try:
                structured = llm.with_structured_output(Diagnosis, method="function_calling")
                result = structured.invoke(build_messages(state))
                if result is None:
                    raise ValueError("model returned no tool call")
                return {"diagnosis": result.model_dump() | {"source": "llm"}}
            except Exception as e:  # noqa: BLE001 - any LLM failure falls back to rules
                log.warning("LLM diagnosis failed (%s), falling back to rules", e)
        return {"diagnosis": rules.fallback_diagnosis(state) | {"source": "rules"}}

    def healthy(state: State) -> State:
        return {"diagnosis": {
            "summary": "Nothing wrong found",
            "root_cause": "All pods are running and ready, no warning events.",
            "confidence": "high", "evidence": [], "fix": [], "commands": [],
            "source": "rules",
        }}

    g = StateGraph(State)
    g.add_node("collect", collect)
    g.add_node("triage", triage)
    g.add_node("investigate", investigate)
    g.add_node("diagnose", diagnose)
    g.add_node("healthy", healthy)

    g.set_entry_point("collect")
    g.add_edge("collect", "triage")
    g.add_conditional_edges(
        "triage",
        lambda s: "investigate" if s["pending"] else ("diagnose" if s["symptoms"] else "healthy"),
    )
    g.add_conditional_edges(
        "investigate",
        lambda s: "investigate" if s["pending"] else "diagnose",
    )
    g.add_edge("diagnose", END)
    g.add_edge("healthy", END)
    return g.compile()
