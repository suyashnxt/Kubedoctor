import argparse
import json
import logging
import sys

from dotenv import load_dotenv

from .graph import build_graph


def describe_step(node: str, update: dict) -> str:
    if node == "collect":
        return f"collected {len(update['pods'])} pod(s), {len(update['events'])} event(s)"
    if node == "triage":
        s = update["symptoms"] or ["none"]
        return f"symptoms: {', '.join(s)} | plan: {', '.join(update['pending']) or '-'}"
    if node == "investigate":
        return f"ran check '{list(update['evidence'])[-1]}'"
    if node == "diagnose":
        return f"diagnosis via {update['diagnosis']['source']}"
    return ""


def print_report(state: dict) -> None:
    d = state["diagnosis"]
    print()
    print(f"{state['kind']}/{state['name']}  (namespace {state['namespace']})")
    if state.get("deployment"):
        dep = state["deployment"]
        print(f"replicas: {dep['ready']}/{dep['replicas']} ready")
    print()
    print(f"  {d['summary']}")
    print(f"  confidence: {d['confidence']}  (source: {d['source']})")
    print()
    print("Root cause")
    print(f"  {d['root_cause']}")
    if d["evidence"]:
        print("\nEvidence")
        for e in d["evidence"]:
            print(f"  - {e}")
    if d["fix"]:
        print("\nFix")
        for i, f in enumerate(d["fix"], 1):
            print(f"  {i}. {f}")
    if d["commands"]:
        print("\nUseful commands")
        for c in d["commands"]:
            print(f"  $ {c}")
    print()


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="kubedoctor", description="Figure out why a pod or deployment is unhappy.")
    p.add_argument("target", help="pod/<name> or deployment/<name>")
    p.add_argument("-n", "--namespace", default="default")
    p.add_argument("--context", help="kubeconfig context to use")
    p.add_argument("--no-llm", action="store_true", help="rules only, no model call")
    p.add_argument("--json", action="store_true", help="print the final state as JSON")
    p.add_argument("-q", "--quiet", action="store_true", help="don't print the step trace")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    if "/" not in args.target:
        p.error("target must look like deployment/checkout or pod/checkout-7d9f8-x2x")
    kind, name = args.target.split("/", 1)
    kind = {"deploy": "deployment", "po": "pod"}.get(kind, kind)

    from .cluster import Cluster  # imported late so --help works without a kubeconfig
    cluster = Cluster(context=args.context)
    llm = None
    if not args.no_llm:
        from .llm import get_llm
        llm = get_llm()

    graph = build_graph(cluster, llm)
    state = {"kind": kind, "name": name, "namespace": args.namespace, "use_llm": not args.no_llm}

    for chunk in graph.stream(state, stream_mode="updates"):
        for node, update in chunk.items():
            state.update(update)
            if not args.quiet and not args.json:
                print(f"[{node:>11}] {describe_step(node, update)}", file=sys.stderr)

    if args.json:
        print(json.dumps({k: state[k] for k in ("symptoms", "diagnosis", "evidence")}, indent=2, default=str))
    else:
        print_report(state)

    return 0 if not state.get("symptoms") else 1


if __name__ == "__main__":
    sys.exit(main())
