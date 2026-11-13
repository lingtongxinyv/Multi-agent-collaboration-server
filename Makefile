# BellHub 常用命令
.PHONY: help install frontend build test dev-panel dev-hub clean

help:
	@echo "make install    安装 Python 依赖（uv sync）"
	@echo "make frontend   安装并构建前端（npm ci && build）"
	@echo "make build      前端构建"
	@echo "make test       跑全部验收（17 套）"
	@echo "make dev-panel  本机起面板 :8010"
	@echo "make dev-hub    本机起 Hub :8765"
	@echo "make deploy     装 systemd + nginx（需 sudo）"
	@echo "make clean      清构建产物与缓存"

install:
	uv sync

frontend:
	cd panel/frontend && npm ci

build: frontend
	cd panel/frontend && npm run build

test:
	bash tests/run_all.sh

dev-panel:
	uv run uvicorn app.main:app --app-dir panel/backend --host 127.0.0.1 --port 8010

dev-hub:
	uv run python mcp-hub/hub.py 127.0.0.1 8765

deploy:
	sudo bash deploy/setup.sh

clean:
	rm -rf panel/frontend/dist panel/frontend/node_modules .venv
	find . -name __pycache__ -prune -exec rm -rf {} +
