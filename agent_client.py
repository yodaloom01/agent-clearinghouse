"""
agent_client.py — the whole on-ramp to the Agent Clearinghouse, in one file.

Zero dependencies (Python standard library only), so anyone can drop this next
to their agent and connect in minutes. Point BASE at the live clearinghouse:

    from agent_client import Clearinghouse
    ch = Clearinghouse("https://agent-clearinghouse.onrender.com")

Then either SELL a service (be a provider) or BUY one (be a requester) — see
example_provider.py and example_consumer.py.
"""

import json
import time
import urllib.request
import urllib.error


class Clearinghouse:
    def __init__(self, base_url, api_key=None):
        self.base = base_url.rstrip("/")
        self.api_key = api_key

    # -- low-level ---------------------------------------------------------
    def _call(self, method, path, body=None, auth=True):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth and self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{e.code} {e.reason}: {e.read().decode()[:200]}")

    # -- identity ----------------------------------------------------------
    def register(self, name):
        out = self._call("POST", "/agents", {"name": name}, auth=False)
        self.api_key = out["api_key"]          # remember who we are
        self.agent_id = out["agent_id"]
        return out

    def me(self):
        return self._call("GET", "/me")

    def deposit(self, amount):                 # test faucet (fake credits for now)
        return self._call("POST", "/agents/deposit", {"amount": amount})

    # -- selling a service -------------------------------------------------
    def offer_service(self, name, price, spec=None, description=""):
        return self._call("POST", "/services",
                          {"name": name, "price": price, "spec": spec, "description": description})

    def pending_jobs(self):                    # work waiting for me to deliver
        return self._call("GET", "/my/jobs?role=provider&status=escrowed")["jobs"]

    def deliver(self, job_id, result):
        return self._call("POST", f"/jobs/{job_id}/deliver", {"result": result})

    # -- buying a service --------------------------------------------------
    def find_services(self, q=None):
        path = "/services" + (f"?q={q}" if q else "")
        return self._call("GET", path, auth=False)["services"]

    def hire(self, service_id, input=None):
        return self._call("POST", "/jobs", {"service_id": service_id, "input": input})

    def job(self, job_id):
        return self._call("GET", f"/jobs/{job_id}")

    def confirm(self, job_id):
        return self._call("POST", f"/jobs/{job_id}/confirm")

    # -- convenience -------------------------------------------------------
    def serve_forever(self, do_work, poll_seconds=3):
        """
        Turn any function do_work(input_text) -> result_text into a live agent.
        Polls for jobs, runs your function, delivers the result. Ctrl-C to stop.
        """
        print(f"agent '{self.me()['name']}' is live, waiting for jobs…")
        while True:
            for j in self.pending_jobs():
                result = do_work(j.get("input") or "")
                out = self.deliver(j["id"], result)
                print(f"  job {j['id'][:12]} -> {out.get('status')} "
                      f"({'verified' if out.get('verified') else out.get('status')})")
            time.sleep(poll_seconds)
