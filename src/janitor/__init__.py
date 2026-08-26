from hosts.janitor import reap_expired_hosts
from templates.janitor import reap_templates


async def reap() -> None:
    await reap_expired_hosts()
    await reap_templates()
