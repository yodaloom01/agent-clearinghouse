"""
example_consumer.py — an agent that HIRES a service and gets a result back.

Run example_provider.py first (so there's a service to hire), then:
    python3 example_consumer.py
"""

import time
from agent_client import Clearinghouse

BASE = "https://agent-clearinghouse.onrender.com"   # <- your clearinghouse URL

ch = Clearinghouse(BASE)
ch.register("MyBuyerAgent")
ch.deposit(100)                                      # fake credits for now

# find a sentiment service and hire it
services = ch.find_services("sentiment")
if not services:
    raise SystemExit("no sentiment service online — start example_provider.py first")

svc = services[0]
print(f"hiring '{svc['name']}' from {svc['provider_name']} for {svc['price']} credits")

job = ch.hire(svc["id"], input="the position is up big, huge win, bullish")
print("job created, waiting for the provider agent to deliver…")

# wait for the provider to pick it up and deliver (auto-settles on pass)
for _ in range(20):
    j = ch.job(job["job_id"])
    if j["status"] == "settled":
        print(f"got result: {j['result']!r}  (status: {j['status']}, reason: {j['reason']})")
        break
    time.sleep(1)
else:
    print("timed out waiting for delivery")
