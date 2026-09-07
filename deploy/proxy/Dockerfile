FROM mitmproxy/mitmproxy:12.2.3

COPY swap.py /addon/swap.py

CMD ["mitmdump", "--listen-host=0.0.0.0", "--listen-port=8880", "--set=flow_detail=1", "-s", "/addon/swap.py"]
