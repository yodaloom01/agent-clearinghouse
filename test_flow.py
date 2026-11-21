"""
Full test of the clearinghouse: verification, deadlines, reputation, and the
cheating attempts each of those is meant to stop. Uses a controllable clock so
timeout paths run instantly.
"""

import os
if os.path.exists("clearinghouse.db"):
    os.remove("clearinghouse.db")

from fastapi.testclient import TestClient
import app as appmod

client = TestClient(appmod.app)

# controllable clock -------------------------------------------------------
CLOCK = {"t": 1_000_000.0}
appmod._now = lambda: CLOCK["t"]
def advance(seconds):
    CLOCK["t"] += seconds


def h(key): return {"X-API-Key": key}
def mk_agent(name):
    a = client.post("/agents", json={"name": name}).json()
    return a["agent_id"], a["api_key"]
def fund(key, amt):
    client.post("/agents/deposit", headers=h(key), json={"amount": amt})


# 1. auto-verified: passing delivery settles instantly, buyer has no say --------
def test_auto_pass_settles_instantly():
    pid, pk = mk_agent("AutoProvider")
    rid, rk = mk_agent("AutoBuyer")
    svc = client.post("/services", headers=h(pk), json={
        "name": "yesno", "price": 40,
        "spec": [{"type": "one_of", "values": ["YES", "NO"]}],
    }).json()
    assert svc["auto_verified"] is True
    fund(rk, 40)
    job = client.post("/jobs", headers=h(rk), json={"service_id": svc["service_id"]}).json()
    assert job["auto_verified"] is True
    d = client.post(f"/jobs/{job['job_id']}/deliver", headers=h(pk), json={"result": "YES"}).json()
    assert d["status"] == "settled" and d["verified"] is True
    # provider paid, buyer spent, nothing escrowed
    assert client.get("/me", headers=h(pk)).json()["balance"] == 40
    assert client.get("/me", headers=h(rk)).json() == {
        "agent_id": rid, "name": "AutoBuyer", "balance": 0, "escrowed": 0}
    # buyer cannot dispute a settled auto job
    assert client.post(f"/jobs/{job['job_id']}/dispute", headers=h(rk)).status_code == 409
    print("  auto-pass settles instantly; dispute blocked  OK")


# 2. auto-verified: bad delivery is rejected, provider retries and passes --------
def test_auto_fail_then_retry():
    pid, pk = mk_agent("RetryProvider")
    rid, rk = mk_agent("RetryBuyer")
    svc = client.post("/services", headers=h(pk), json={
        "name": "summary", "price": 30,
        "spec": [{"type": "min_sentences", "value": 2}, {"type": "max_length", "value": 200}],
    }).json()
    fund(rk, 30)
    job = client.post("/jobs", headers=h(rk), json={"service_id": svc["service_id"]}).json()
    jid = job["job_id"]
    # too short: one sentence -> fail, stays escrowed, no payment
    bad = client.post(f"/jobs/{jid}/deliver", headers=h(pk), json={"result": "Just one."}).json()
    assert bad["status"] == "escrowed" and bad["verified"] is False
    assert client.get("/me", headers=h(pk)).json()["balance"] == 0
    # good retry -> settles
    good = client.post(f"/jobs/{jid}/deliver", headers=h(pk),
                       json={"result": "First sentence here. Second one too."}).json()
    assert good["status"] == "settled"
    assert client.get("/me", headers=h(pk)).json()["balance"] == 30
    assert client.get(f"/jobs/{jid}", headers=h(pk)).json()["attempts"] == 2
    print("  auto-fail rejected, retry settles, attempts counted  OK")


# 3. seller no-show: deadline passes -> buyer refunded (money never stuck) -------
def test_provider_timeout_refunds_buyer():
    pid, pk = mk_agent("GhostProvider")
    rid, rk = mk_agent("StuckBuyer")
    svc = client.post("/services", headers=h(pk), json={
        "name": "slow", "price": 25, "deadline_seconds": 100,
        "spec": [{"type": "not_empty"}],
    }).json()
    fund(rk, 25)
    job = client.post("/jobs", headers=h(rk), json={"service_id": svc["service_id"]}).json()
    jid = job["job_id"]
    assert client.get("/me", headers=h(rk)).json()["escrowed"] == 25
    advance(101)  # blow the deadline
    r = client.post(f"/jobs/{jid}/reclaim", headers=h(rk)).json()
    assert r["status"] == "refunded"
    me = client.get("/me", headers=h(rk)).json()
    assert me["balance"] == 25 and me["escrowed"] == 0
    # provider that ghosted now carries a timeout on record
    rep = client.get(f"/agents/{pid}/reputation").json()
    assert rep["as_provider"]["timed_out"] == 1
    # too-late delivery is refused
    assert client.post(f"/jobs/{jid}/deliver", headers=h(pk), json={"result": "x"}).status_code == 409
    print("  provider timeout refunds buyer; late deliver blocked  OK")


# 4. buyer ghosts a delivered no-spec job -> seller auto-paid via sweep ----------
def test_review_timeout_pays_seller():
    pid, pk = mk_agent("HonestProvider")
    rid, rk = mk_agent("GhostBuyer")
    svc = client.post("/services", headers=h(pk), json={
        "name": "artwork", "price": 60, "review_seconds": 100,  # no spec -> manual
    }).json()
    assert svc["auto_verified"] is False
    fund(rk, 60)
    job = client.post("/jobs", headers=h(rk), json={"service_id": svc["service_id"]}).json()
    jid = job["job_id"]
    d = client.post(f"/jobs/{jid}/deliver", headers=h(pk), json={"result": "a lovely logo"}).json()
    assert d["status"] == "delivered"
    advance(101)  # buyer never responds
    swept = client.post("/maintenance/sweep").json()
    assert jid in swept["settled"]
    assert client.get("/me", headers=h(pk)).json()["balance"] == 60
    print("  buyer ghost -> seller auto-paid after review window  OK")


# 5. manual confirm and honest dispute, both recorded in reputation -------------
def test_manual_confirm_and_dispute_reputation():
    pid, pk = mk_agent("Painter")
    good_id, good_k = mk_agent("FairBuyer")
    bad_id, bad_k = mk_agent("SerialDisputer")
    svc = client.post("/services", headers=h(pk),
                      json={"name": "portrait", "price": 20}).json()  # no spec
    # fair buyer confirms
    fund(good_k, 20)
    j1 = client.post("/jobs", headers=h(good_k), json={"service_id": svc["service_id"]}).json()
    client.post(f"/jobs/{j1['job_id']}/deliver", headers=h(pk), json={"result": "done"})
    c = client.post(f"/jobs/{j1['job_id']}/confirm", headers=h(good_k)).json()
    assert c["status"] == "settled"
    # serial disputer refunds twice
    fund(bad_k, 40)
    for _ in range(2):
        j = client.post("/jobs", headers=h(bad_k), json={"service_id": svc["service_id"]}).json()
        client.post(f"/jobs/{j['job_id']}/deliver", headers=h(pk), json={"result": "done"})
        client.post(f"/jobs/{j['job_id']}/dispute", headers=h(bad_k))
    rep = client.get(f"/agents/{bad_id}/reputation").json()
    assert rep["as_requester"]["disputed"] == 2
    assert rep["as_requester"]["dispute_rate"] == 1.0  # disputes everything -> visible
    good_rep = client.get(f"/agents/{good_id}/reputation").json()
    assert good_rep["as_requester"]["dispute_rate"] == 0.0
    print("  confirm settles; serial disputer flagged (rate 1.0)  OK")


# 6. guard rails ----------------------------------------------------------------
def test_guards():
    pid, pk = mk_agent("Guard")
    svc = client.post("/services", headers=h(pk),
                      json={"name": "x", "price": 10, "spec": [{"type": "not_empty"}]}).json()
    assert client.post("/jobs", headers=h(pk),
                       json={"service_id": svc["service_id"]}).status_code == 400  # self-hire
    _, broke = mk_agent("Broke")
    assert client.post("/jobs", headers=h(broke),
                       json={"service_id": svc["service_id"]}).status_code == 402  # no funds
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers=h("sk_bad")).status_code == 401
    # malformed spec rejected at listing
    assert client.post("/services", headers=h(pk),
                       json={"name": "y", "price": 1, "spec": [{"nope": 1}]}).status_code == 400
    print("  guards: self-hire 400, broke 402, auth 401, bad spec 400  OK")


if __name__ == "__main__":
    test_auto_pass_settles_instantly()
    test_auto_fail_then_retry()
    test_provider_timeout_refunds_buyer()
    test_review_timeout_pays_seller()
    test_manual_confirm_and_dispute_reputation()
    test_guards()
    print("\nALL TESTS PASSED")
