import asyncio

from secret_proxy.server import SecretProxyServer
from secret_proxy.settings import SecretProxySettings


def main() -> None:
    server = SecretProxyServer(SecretProxySettings())
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
