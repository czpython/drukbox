# Cron entry point: `python -m janitor`.
import asyncio
import logging

from janitor import reap

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
asyncio.run(reap())
