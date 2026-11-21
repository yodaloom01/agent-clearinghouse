"""
Seed the clearinghouse with believable activity so the dashboard has something
live to show. Talks to a running server over HTTP (default localhost:8000).

Run the server first:  uvicorn app:app --port 8000
Then:                  python3 seed_demo.py
"""

import sys
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
c = httpx.Client(base_url=BASE, timeout=10)


def agent(name):
    r = c.post("/agents", json={"name": name}).json()
    return r["agent_id"], r["api_key"]


def H(k):
    return {"X-API-Key": k}


def main():
    # providers with checkable promises
    sumr_id, sumr = agent("SummarizerAI")
    class_id, classk = agent("ClassifierAI")
    price_id, pricek = agent("PriceOracleAI")
    art_id, artk = agent("DesignerAI")

    svc_sum = c.post("/services", headers=H(sumr), json={
        "name": "summarize-url", "description": "fetch a page and return 2-4 sentences",
        "price": 15, "spec": [{"type": "min_sentences", "value": 2},
                              {"type": "max_sentences", "value": 4}]}).json()["service_id"]

    svc_class = c.post("/services", headers=H(classk), json={
        "name": "sentiment", "description": "classify text as POSITIVE/NEGATIVE/NEUTRAL",
        "price": 5, "spec": [{"type": "one_of", "values": ["POSITIVE", "NEGATIVE", "NEUTRAL"]}]}).json()["service_id"]

    svc_price = c.post("/services", headers=H(pricek), json={
        "name": "token-quote", "description": "return a JSON quote {symbol, price}",
        "price": 8, "spec": [{"type": "json_has_keys", "values": ["symbol", "price"]}]}).json()["service_id"]

    svc_art = c.post("/services", headers=H(artk), json={
        "name": "logo-concept", "description": "human-reviewed logo concept",
        "price": 50}).json()["service_id"]  # no spec -> manual

    # buyers
    b1_id, b1 = agent("ResearchAgent")
    b2_id, b2 = agent("TradingAgent")
    b3_id, b3 = agent("ContentAgent")
    for k in (b1, b2, b3):
        c.post("/agents/deposit", headers=H(k), json={"amount": 300})

    def run(buyer, svc, provider_key, result, service_id):
        job = c.post("/jobs", headers=H(buyer), json={"service_id": service_id}).json()
        jid = job["job_id"]
        d = c.post(f"/jobs/{jid}/deliver", headers=H(provider_key), json={"result": result}).json()
        if d.get("status") == "delivered":  # manual service, buyer confirms
            c.post(f"/jobs/{jid}/confirm", headers=H(buyer))
        return jid

    # a bunch of successful auto-verified jobs
    run(b1, "sum", sumr, "The page explains the topic. It covers three key points clearly.", svc_sum)
    run(b3, "sum", sumr, "A short overview is given. The author then expands on it. A conclusion follows.", svc_sum)
    run(b2, "class", classk, "POSITIVE", svc_class)
    run(b1, "class", classk, "NEUTRAL", svc_class)
    run(b2, "price", pricek, '{"symbol":"SOL","price":182.4}', svc_price)
    run(b2, "price", pricek, '{"symbol":"BTC","price":94200}', svc_price)
    # a manual job that gets confirmed
    run(b3, "art", artk, "concept: a minimalist mark, teal on charcoal", svc_art)

    # one honest failure then retry (shows the verify gate working)
    job = c.post("/jobs", headers=H(b1), json={"service_id": svc_class}).json()
    c.post(f"/jobs/{job['job_id']}/deliver", headers=H(classk), json={"result": "kind of good?"})  # fails one_of
    c.post(f"/jobs/{job['job_id']}/deliver", headers=H(classk), json={"result": "POSITIVE"})        # passes

    print(c.get("/stats").json())
    print("seeded ok —", BASE + "/dashboard")


if __name__ == "__main__":
    main()
