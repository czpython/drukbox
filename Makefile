LOCAL_ENV = \
	DATABASE_URL=sqlite+aiosqlite:///./drukbox.db \
	SERVICE_TOKENS=dev-token \
	DEFAULT_HOST_PROVIDER=docker \
	TAILSCALE_ENABLED=false

.PHONY: dev
dev:
	$(LOCAL_ENV) uv run alembic upgrade head
	$(LOCAL_ENV) uv run python -m secrets_exchange & $(LOCAL_ENV) uv run uvicorn api.app:app
