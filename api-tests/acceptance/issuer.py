"""A dummy issuer: GET /mint/<service> answers the JSON in <service>.json from DIRECTORY.

usage: ISSUER_BEARER=<bearer> issuer.py PORT [DIRECTORY]

Control, behind the same bearer: GET /fetches counts the mint requests per
service. POST /fail with {"failing": true} makes every mint answer 500 until
{"failing": false}.
"""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
BEARER = os.environ.get("ISSUER_BEARER") or sys.exit("set ISSUER_BEARER")
DIRECTORY = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(os.path.abspath(__file__))
state: dict = {"fetches": 0, "services": {}, "failing": False}


class Issuer(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not self.is_authorized():
            return self.answer(403)
        if self.path == "/fetches":
            return self.answer(200, {"fetches": state["fetches"], "services": state["services"]})
        if match := re.fullmatch(r"/mint/([a-z0-9-]+)", self.path):
            return self.mint(match.group(1))
        return self.answer(404)

    def do_POST(self) -> None:
        if not self.is_authorized():
            return self.answer(403)
        if self.path != "/fail":
            return self.answer(404)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")) or b"{}"))
        state["failing"] = bool(body.get("failing"))
        print(f"failing={state['failing']}", flush=True)
        return self.answer(200, {"failing": state["failing"]})

    def mint(self, service: str) -> None:
        state["fetches"] += 1
        state["services"][service] = state["services"].get(service, 0) + 1
        print(f"fetch #{state['fetches']} {service} failing={state['failing']}", flush=True)
        if state["failing"]:
            return self.answer(500)
        try:
            with open(os.path.join(DIRECTORY, f"{service}.json")) as file:
                return self.answer(200, json.load(file))
        except FileNotFoundError:
            print(f"no {service}.json in {DIRECTORY}", flush=True)
            return self.answer(404)

    def is_authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {BEARER}"

    def answer(self, status: int, body: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if body:
            self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args: object) -> None:
        pass


HTTPServer(("127.0.0.1", PORT), Issuer).serve_forever()
