import uvicorn

from secrets_exchange.settings import SecretsExchangeSettings

settings = SecretsExchangeSettings()
uvicorn.run("secrets_exchange.app:app", host=settings.bind_host, port=settings.port)
