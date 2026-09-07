FROM mitmproxy/mitmproxy:12.2.3

COPY swap.py /addon/swap.py

# A host-networked deployment binds one interface. It sets the variable and
# keeps the command.
CMD ["sh", "-c", "exec mitmdump --listen-host=\"${SECRETS_PROXY_BIND_HOST:-0.0.0.0}\" --listen-port=8880 --set=flow_detail=1 -s /addon/swap.py"]
