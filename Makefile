build:
	docker build -t ghcr.io/runcodes-icmc/compiler-engine:latest -f ./Dockerfile .

all: build

.PHONY: all
