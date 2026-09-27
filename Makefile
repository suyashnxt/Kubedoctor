CLUSTER ?= kd

.PHONY: cluster demo clean test

cluster:
	kind create cluster --name $(CLUSTER)

demo:
	kubectl apply -f examples/00-namespace.yaml
	kubectl apply -f examples/
	@echo "give it ~60s for things to start breaking, then: kubectl -n kd-demo get pods"

clean:
	kubectl delete namespace kd-demo --ignore-not-found

test:
	python -m pytest -q
