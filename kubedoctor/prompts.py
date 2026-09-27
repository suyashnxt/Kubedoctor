import json
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field


class Diagnosis(BaseModel):
    summary: str = Field(description="One sentence: what is wrong")
    root_cause: str = Field(description="Why it is happening, in 1-3 sentences")
    confidence: Literal["low", "medium", "high"]
    evidence: list[str] = Field(description="Exact log lines, event messages or spec fields that back up the root cause")
    fix: list[str] = Field(description="Concrete steps to fix it, most likely fix first")
    commands: list[str] = Field(default_factory=list, description="kubectl commands to verify or apply the fix")


SYSTEM = """You are helping an on-call engineer debug a Kubernetes workload.
You get pod status, recent events and whatever extra evidence was collected.
Work out the most likely root cause.

Rules:
- Only use facts that are in the data. If the data doesn't show the cause, say so and set confidence to low.
- Quote the exact log line or event message that points to the cause.
- Prefer the fix a person would actually apply (fix the tag, raise the limit, create the missing ConfigMap)
  over generic advice like "check the logs".
- kubectl commands must use the real namespace and resource names from the data."""


def _section(title: str, data, limit: int = 6000) -> str:
    text = data if isinstance(data, str) else json.dumps(data, indent=1, default=str)
    if len(text) > limit:
        text = text[:limit] + "\n...(truncated)"
    return f"## {title}\n{text}\n"


def build_messages(state: dict) -> list:
    parts = [
        f"Target: {state['kind']}/{state['name']} in namespace {state['namespace']}",
        f"Symptoms flagged by rules: {', '.join(state['symptoms'])}\n",
    ]
    if state.get("deployment"):
        parts.append(_section("Deployment status", state["deployment"]))
    parts.append(_section("Pods", state["pods"]))
    parts.append(_section("Events", state["events"]))
    for check, result in state["evidence"].items():
        parts.append(_section(f"Evidence: {check}", result))
    return [SystemMessage(SYSTEM), HumanMessage("\n".join(parts))]
