import logging

import uvicorn

from secrets_exchange.settings import SecretsExchangeSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
settings = SecretsExchangeSettings()
uvicorn.run("secrets_exchange.app:app", host=settings.bind_host, port=settings.port)
