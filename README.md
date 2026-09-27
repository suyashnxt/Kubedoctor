# kubedoctor

A small LangGraph agent that looks at a broken pod or deployment and tells you why it's broken.

```
$ python -m kubedoctor deployment/payments-api -n kd-demo
[    collect] collected 2 pod(s), 6 event(s)
[     triage] symptoms: crashloop | plan: previous_logs, logs, spec
[investigate] ran check 'previous_logs'
[investigate] ran check 'logs'
[investigate] ran check 'spec'
[   diagnose] diagnosis via rules

deployment/payments-api  (namespace kd-demo)
replicas: 0/2 ready

  Container keeps crashing on startup
  confidence: medium  (source: rules)

Root cause
  Process exits with code 1. Last error in logs: FATAL: DATABASE_URL is not set, refusing to start
...
```

## Why

Most pages I've been woken up for on Kubernetes fall into the same handful of buckets: crashloop, OOMKilled, bad image tag, failing probe, pod stuck in Pending, missing ConfigMap. The first ten minutes of each one is identical. You run `get pods`, `describe`, `logs --previous` and `get events`, then squint.

I wanted to see how much of that first ten minutes an agent could do, and to learn LangGraph properly while doing it. This is a cleaned-up, standalone version of that idea that anyone can run on a laptop.

## How it works

```
collect -> triage --(healthy)--> healthy -> END
              |
              v
         investigate <--+   one check per step,
              |         |   loops until the plan is empty
              +---------+
              |
              v
          diagnose -> END
```

- **collect**: pulls pod status and events for the target (and for up to 2 unhealthy pods).
- **triage**: plain Python rules (`rules.py`) turn the status into symptoms like `crashloop` or `oomkilled`, then build a plan of checks. A crashloop gets previous logs, current logs and the container spec. An unschedulable pod gets the spec and node capacity. Nobody needs node info for a typo in an image tag.
- **investigate**: runs one check per step and loops back to itself until the plan is done. Doing one check per node makes the trace readable, and makes it easy to add a new check.
- **diagnose**: sends everything to the LLM and asks for a structured `Diagnosis` (summary, root cause, confidence, evidence, fix, commands). If there's no LLM, or the call fails, it falls back to rule-based diagnosis.

The split matters. The rules decide **what to look at**. The LLM only decides **what it means**. That keeps the agent predictable and the prompt small, and you can run it with `--no-llm` and still get something useful.

Things I deliberately don't send to the model:
- env var **values** (only names; they're often secrets)
- logs from more than 2 pods

## Running it locally

You need Docker, [kind](https://kind.sigs.k8s.io/), kubectl and Python 3.10+.

```bash
git clone https://github.com/suyashnxt/kubedoctor.git
cd kubedoctor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

kind create cluster --name kd
make demo            # applies the broken workloads in examples/
```

### LLM (optional)

The default is a local model through [Ollama](https://ollama.com), so it costs nothing:

```bash
ollama pull qwen2.5:7b
cp .env.example .env
```

Any OpenAI-compatible endpoint works. Set `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` in `.env`. Small local models are fine for the obvious cases. For messy real-world logs, a bigger model is noticeably better.

Pick a model that supports tool calling (qwen2.5, llama3.1 and mistral-nemo all do). The structured output goes through function calling.

### Try the broken examples

Wait a minute after `make demo`, then:

| Command | What's wrong |
|---|---|
| `python -m kubedoctor deployment/payments-api -n kd-demo` | exits because an env var is missing |
| `python -m kubedoctor deployment/report-worker -n kd-demo` | OOMKilled, 64Mi limit |
| `python -m kubedoctor deployment/web -n kd-demo` | typo in the image tag |
| `python -m kubedoctor deployment/frontend -n kd-demo` | readiness probe on a path that 404s |
| `python -m kubedoctor deployment/search-indexer -n kd-demo` | requests 200Gi of memory |
| `python -m kubedoctor deployment/notifications -n kd-demo` | ConfigMap doesn't exist |

Add `--no-llm` to see what the rules alone come up with, then compare it to the LLM version. That comparison taught me more than anything else in this project.

Other flags:

```
--json          machine-readable output
-q              hide the step trace
--context NAME  kubeconfig context
```

The exit code is 0 when nothing is wrong and 1 when something is, so you can script it.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/test_graph.py` runs the full graph against a fake cluster. It's the easiest place to start if you want to see the state change node by node without a real cluster.

## Layout

```
kubedoctor/
  cluster.py   Kubernetes API calls, returns plain dicts
  rules.py     symptom detection, check planning, fallback diagnosis
  graph.py     the LangGraph state machine
  prompts.py   system prompt + Diagnosis schema
  llm.py       ChatOpenAI pointed at whatever endpoint you configure
  __main__.py  CLI
examples/      broken workloads to practise on
```

## Adding your own check

1. Add a method to `Cluster` that returns something JSON-serialisable.
2. Add a branch for it in `run_check()` in `graph.py`.
3. Map a symptom to it in `CHECKS` in `rules.py`.

That's it. The LLM prompt picks up any evidence automatically.

## Known limitations

- Looks at one workload at a time. It won't notice that the real problem is the database pod in another namespace.
- No StatefulSet/DaemonSet/Job support yet (the logic is the same, it's just the pod lookup).
- Events expire after an hour by default, so old failures lose most of their context.
- The rule-based fallback is intentionally simple. It catches the common cases, not the weird ones.

## Ideas / TODO

- [ ] StatefulSet and Job support
- [ ] follow Service -> Endpoints when a pod is fine but traffic isn't reaching it
- [ ] let the LLM request one extra check if it's unsure (bounded, not open-ended)
- [ ] Slack output

## License

MIT
