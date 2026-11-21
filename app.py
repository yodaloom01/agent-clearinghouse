"""
Agent Clearinghouse — spine + verification, deadlines, and reputation.

A place where autonomous agents discover each other's services, hire each other,
and settle payment through escrow so money only moves when work is delivered and
*verified*.

Design guarantees this version enforces:
  - Money can never get stuck. Every job has a delivery deadline (seller no-show ->
    buyer refunded) and, for human-judged jobs, a review window (buyer ghosts ->
    seller paid). Enforced lazily on access and by an explicit sweep.
  - A buyer cannot steal good work. If a service posts a checkable spec, a passing
    delivery settles instantly with no buyer discretion to abuse.
  - Everyone has a reputation. Sellers and buyers both accrue a permanent record,
    so garbage-sellers and serial-refund buyers are both visible before you deal.

v1 uses an internal credit ledger behind hold/release/refund, so a real USDC/x402
settlement layer swaps in without touching discovery, jobs, verification, or rep.

Known residual risks (honest list, not yet closed in code):
  - Sybil reputation farming: an owner of two agents can trade with themselves to
    inflate a record. Mitigated by scoring distinct counterparties and by the fact
    that real settlement costs real value; not eliminated.
  - Subjective quality on no-spec services still needs a judge. The manual path +
    reputation contains the damage; an LLM-judge layer is the next build.
  - Adversarial regex in a spec (ReDoS) is blunted by length caps, not sandboxed.
"""

import secrets
import sqlite3
import time
import json as _json
from contextlib import contextmanager
from typing import Optional, List

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import verification as V

DB_PATH = "clearinghouse.db"

app = FastAPI(title="Agent Clearinghouse", version="0.2.0")


def _now() -> float:
    """Indirection so tests can control the clock."""
    return time.time()


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id        TEXT PRIMARY KEY,
                name      TEXT NOT NULL,
                api_key   TEXT NOT NULL UNIQUE,
                balance   INTEGER NOT NULL DEFAULT 0,
                escrowed  INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS services (
                id          TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL REFERENCES agents(id),
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price       INTEGER NOT NULL,
                endpoint    TEXT,
                spec        TEXT,                        -- JSON list of checks, or NULL
                deadline_seconds INTEGER NOT NULL,        -- provider must deliver within
                review_seconds   INTEGER NOT NULL,        -- buyer review window (no-spec only)
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                service_id    TEXT NOT NULL REFERENCES services(id),
                requester_id  TEXT NOT NULL REFERENCES agents(id),
                provider_id   TEXT NOT NULL REFERENCES agents(id),
                amount        INTEGER NOT NULL,
                status        TEXT NOT NULL,             -- escrowed|delivered|settled|refunded
                reason        TEXT,                      -- why it ended (settlement/timeout/dispute)
                auto          INTEGER NOT NULL,          -- 1 if service had a spec
                input         TEXT,
                result        TEXT,
                verification  TEXT,                      -- JSON report of last delivery attempt
                attempts      INTEGER NOT NULL DEFAULT 0,
                deadline_at   REAL NOT NULL,             -- deliver-by
                review_deadline_at REAL,                 -- confirm-by (set on delivery, no-spec)
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id  TEXT NOT NULL REFERENCES agents(id),
                delta     INTEGER NOT NULL,
                reason    TEXT NOT NULL,
                job_id    TEXT,
                ts        REAL NOT NULL
            );
            """
        )


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def record(conn, agent_id, delta, reason, job_id=None):
    conn.execute(
        "INSERT INTO ledger (agent_id, delta, reason, job_id, ts) VALUES (?,?,?,?,?)",
        (agent_id, delta, reason, job_id, _now()),
    )


# --------------------------------------------------------------------------
# escrow primitives — the only place that touches "money"
# --------------------------------------------------------------------------
def hold_funds(conn, agent_id, amount, job_id):
    row = conn.execute("SELECT balance FROM agents WHERE id=?", (agent_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "agent not found")
    if row["balance"] < amount:
        raise HTTPException(402, "insufficient balance to fund job")
    conn.execute(
        "UPDATE agents SET balance = balance - ?, escrowed = escrowed + ? WHERE id=?",
        (amount, amount, agent_id),
    )
    record(conn, agent_id, -amount, "escrow_hold", job_id)


def release_funds(conn, requester_id, provider_id, amount, job_id):
    conn.execute("UPDATE agents SET escrowed = escrowed - ? WHERE id=?", (amount, requester_id))
    conn.execute("UPDATE agents SET balance = balance + ? WHERE id=?", (amount, provider_id))
    record(conn, provider_id, amount, "job_payment", job_id)


def refund_funds(conn, requester_id, amount, job_id):
    conn.execute(
        "UPDATE agents SET escrowed = escrowed - ?, balance = balance + ? WHERE id=?",
        (amount, amount, requester_id),
    )
    record(conn, requester_id, amount, "escrow_refund", job_id)


# --------------------------------------------------------------------------
# lazy expiry — any time we touch a job, bring it up to date
# --------------------------------------------------------------------------
def _settle_job(conn, job, reason):
    release_funds(conn, job["requester_id"], job["provider_id"], job["amount"], job["id"])
    conn.execute("UPDATE jobs SET status='settled', reason=?, updated_at=? WHERE id=?",
                 (reason, _now(), job["id"]))


def _refund_job(conn, job, reason):
    refund_funds(conn, job["requester_id"], job["amount"], job["id"])
    conn.execute("UPDATE jobs SET status='refunded', reason=?, updated_at=? WHERE id=?",
                 (reason, _now(), job["id"]))


def _mature(conn, job) -> dict:
    """Apply any due timeout to a single job, return its fresh row as dict."""
    now = _now()
    if job["status"] == "escrowed" and now >= job["deadline_at"]:
        # provider never delivered a passing result in time -> buyer made whole
        _refund_job(conn, job, "provider_timeout")
    elif job["status"] == "delivered" and job["review_deadline_at"] is not None \
            and now >= job["review_deadline_at"]:
        # buyer ghosted a delivered job -> provider paid
        _settle_job(conn, job, "review_timeout")
    return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
def current_agent(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(401, "missing X-API-Key")
    with db() as conn:
        row = conn.execute("SELECT * FROM agents WHERE api_key=?", (x_api_key,)).fetchone()
    if row is None:
        raise HTTPException(401, "invalid API key")
    return dict(row)


# --------------------------------------------------------------------------
# reputation
# --------------------------------------------------------------------------
def reputation_for(conn, agent_id) -> dict:
    as_provider = conn.execute(
        "SELECT status, reason FROM jobs WHERE provider_id=?", (agent_id,)
    ).fetchall()
    as_requester = conn.execute(
        "SELECT status, reason, provider_id FROM jobs WHERE requester_id=?", (agent_id,)
    ).fetchall()

    prov_total = len(as_provider)
    prov_settled = sum(1 for j in as_provider if j["status"] == "settled")
    prov_timeout = sum(1 for j in as_provider if j["reason"] == "provider_timeout")
    prov_disputed = sum(1 for j in as_provider if j["reason"] == "disputed")

    req_total = len(as_requester)
    req_disputed = sum(1 for j in as_requester if j["reason"] == "disputed")
    distinct_providers = len({j["provider_id"] for j in as_requester})

    def rate(n, d):
        return round(n / d, 3) if d else None

    return {
        "agent_id": agent_id,
        "as_provider": {
            "jobs": prov_total,
            "settled": prov_settled,
            "settle_rate": rate(prov_settled, prov_total),
            "timed_out": prov_timeout,
            "disputed_against": prov_disputed,
        },
        "as_requester": {
            "jobs": req_total,
            "disputed": req_disputed,
            "dispute_rate": rate(req_disputed, req_total),
            "distinct_providers": distinct_providers,  # sybil signal: low vs jobs = suspicious
        },
    }


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------
class RegisterIn(BaseModel):
    name: str


class DepositIn(BaseModel):
    amount: int = Field(gt=0)


class ServiceIn(BaseModel):
    name: str
    description: str = ""
    price: int = Field(ge=0)
    endpoint: Optional[str] = None
    spec: Optional[List[dict]] = None          # checkable promise; None -> manual review
    deadline_seconds: int = Field(default=3600, gt=0)
    review_seconds: int = Field(default=86400, gt=0)


class JobIn(BaseModel):
    service_id: str
    input: Optional[str] = None


class DeliverIn(BaseModel):
    result: str


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------
@app.post("/agents")
def register(body: RegisterIn):
    agent_id = new_id("agent")
    api_key = "sk_" + secrets.token_hex(16)
    with db() as conn:
        conn.execute(
            "INSERT INTO agents (id, name, api_key, balance, escrowed, created_at) VALUES (?,?,?,0,0,?)",
            (agent_id, body.name, api_key, _now()),
        )
    return {"agent_id": agent_id, "api_key": api_key, "name": body.name}


@app.post("/agents/deposit")
def deposit(body: DepositIn, me=Depends(current_agent)):
    with db() as conn:
        conn.execute("UPDATE agents SET balance = balance + ? WHERE id=?", (body.amount, me["id"]))
        record(conn, me["id"], body.amount, "deposit")
        row = conn.execute("SELECT balance, escrowed FROM agents WHERE id=?", (me["id"],)).fetchone()
    return {"balance": row["balance"], "escrowed": row["escrowed"]}


@app.get("/me")
def me(me=Depends(current_agent)):
    return {"agent_id": me["id"], "name": me["name"],
            "balance": me["balance"], "escrowed": me["escrowed"]}


@app.get("/agents/{agent_id}/reputation")
def get_reputation(agent_id: str):
    with db() as conn:
        if conn.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone() is None:
            raise HTTPException(404, "agent not found")
        return reputation_for(conn, agent_id)


# --------------------------------------------------------------------------
# services (discovery)
# --------------------------------------------------------------------------
@app.post("/services")
def create_service(body: ServiceIn, me=Depends(current_agent)):
    err = V.validate_spec(body.spec)
    if err:
        raise HTTPException(400, f"bad spec: {err}")
    sid = new_id("svc")
    with db() as conn:
        conn.execute(
            "INSERT INTO services (id, provider_id, name, description, price, endpoint, spec, "
            "deadline_seconds, review_seconds, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
            (sid, me["id"], body.name, body.description, body.price, body.endpoint,
             _json.dumps(body.spec) if body.spec else None,
             body.deadline_seconds, body.review_seconds, _now()),
        )
    return {"service_id": sid, "name": body.name, "price": body.price,
            "auto_verified": bool(body.spec)}


@app.get("/services")
def list_services(q: Optional[str] = None):
    with db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.name, s.description, s.price, s.endpoint, s.spec, "
            "s.deadline_seconds, s.review_seconds, s.provider_id, a.name AS provider_name "
            "FROM services s JOIN agents a ON a.id = s.provider_id "
            "WHERE s.active = 1 ORDER BY s.created_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["spec"] = _json.loads(d["spec"]) if d["spec"] else None
            d["auto_verified"] = d["spec"] is not None
            d["provider_reputation"] = reputation_for(conn, d["provider_id"])["as_provider"]
            out.append(d)
    if q:
        ql = q.lower()
        out = [r for r in out if ql in r["name"].lower() or ql in r["description"].lower()]
    return {"services": out}


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
@app.post("/jobs")
def create_job(body: JobIn, me=Depends(current_agent)):
    with db() as conn:
        svc = conn.execute("SELECT * FROM services WHERE id=? AND active=1", (body.service_id,)).fetchone()
        if svc is None:
            raise HTTPException(404, "service not found")
        if svc["provider_id"] == me["id"]:
            raise HTTPException(400, "cannot hire your own service")

        job_id = new_id("job")
        amount = svc["price"]
        hold_funds(conn, me["id"], amount, job_id)
        now = _now()
        conn.execute(
            "INSERT INTO jobs (id, service_id, requester_id, provider_id, amount, status, reason, "
            "auto, input, result, verification, attempts, deadline_at, review_deadline_at, "
            "created_at, updated_at) VALUES (?,?,?,?,?,'escrowed',NULL,?,?,NULL,NULL,0,?,NULL,?,?)",
            (job_id, svc["id"], me["id"], svc["provider_id"], amount,
             1 if svc["spec"] else 0, body.input, now + svc["deadline_seconds"], now, now),
        )
    return {"job_id": job_id, "status": "escrowed", "amount": amount,
            "provider_id": svc["provider_id"], "auto_verified": bool(svc["spec"]),
            "deliver_by": now + svc["deadline_seconds"]}


@app.post("/jobs/{job_id}/deliver")
def deliver(job_id: str, body: DeliverIn, me=Depends(current_agent)):
    with db() as conn:
        raw = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if raw is None:
            raise HTTPException(404, "job not found")
        job = _mature(conn, raw)  # apply any due timeout first
        if job["provider_id"] != me["id"]:
            raise HTTPException(403, "only the provider can deliver")
        if job["status"] != "escrowed":
            raise HTTPException(409, f"job is {job['status']} ({job['reason']}); cannot deliver")

        svc = conn.execute("SELECT * FROM services WHERE id=?", (job["service_id"],)).fetchone()
        spec = _json.loads(svc["spec"]) if svc["spec"] else None
        conn.execute("UPDATE jobs SET attempts = attempts + 1 WHERE id=?", (job_id,))

        if spec:  # auto-verified path
            passed, report = V.run_checks(body.result, spec)
            conn.execute("UPDATE jobs SET result=?, verification=?, updated_at=? WHERE id=?",
                         (body.result, _json.dumps(report), _now(), job_id))
            if passed:
                fresh = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                _settle_job(conn, fresh, "verified")
                return {"job_id": job_id, "status": "settled", "verified": True,
                        "paid": job["amount"], "report": report}
            # failed: stays escrowed, provider may retry until deadline
            return {"job_id": job_id, "status": "escrowed", "verified": False,
                    "report": report, "note": "checks failed; retry before deadline"}

        # manual path: no spec -> buyer reviews within review window
        now = _now()
        conn.execute(
            "UPDATE jobs SET status='delivered', result=?, review_deadline_at=?, updated_at=? WHERE id=?",
            (body.result, now + svc["review_seconds"], now, job_id),
        )
        return {"job_id": job_id, "status": "delivered", "verified": None,
                "confirm_by": now + svc["review_seconds"]}


@app.post("/jobs/{job_id}/confirm")
def confirm(job_id: str, me=Depends(current_agent)):
    with db() as conn:
        raw = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if raw is None:
            raise HTTPException(404, "job not found")
        job = _mature(conn, raw)
        if job["requester_id"] != me["id"]:
            raise HTTPException(403, "only the requester can confirm")
        if job["status"] == "settled":
            return {"job_id": job_id, "status": "settled", "reason": job["reason"]}
        if job["status"] != "delivered":
            raise HTTPException(409, f"job is {job['status']}; cannot confirm")
        _settle_job(conn, job, "confirmed")
    return {"job_id": job_id, "status": "settled", "paid": job["amount"]}


@app.post("/jobs/{job_id}/dispute")
def dispute(job_id: str, me=Depends(current_agent)):
    """
    Buyer rejects a manually-reviewed delivery -> full refund, recorded against the
    buyer's reputation (so serial disputers are visible) and the seller's.
    Auto-verified jobs cannot be disputed: a passed spec already settled, and the
    buyer agreed to that spec when hiring.
    """
    with db() as conn:
        raw = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if raw is None:
            raise HTTPException(404, "job not found")
        job = _mature(conn, raw)
        if job["requester_id"] != me["id"]:
            raise HTTPException(403, "only the requester can dispute")
        if job["auto"]:
            raise HTTPException(409, "auto-verified jobs settle on passing checks; nothing to dispute")
        if job["status"] not in ("escrowed", "delivered"):
            raise HTTPException(409, f"job is {job['status']}; cannot dispute")
        _refund_job(conn, job, "disputed")
    return {"job_id": job_id, "status": "refunded", "refunded": job["amount"]}


@app.post("/jobs/{job_id}/reclaim")
def reclaim(job_id: str, me=Depends(current_agent)):
    """Buyer pulls their money back if the seller blew the delivery deadline."""
    with db() as conn:
        raw = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if raw is None:
            raise HTTPException(404, "job not found")
        job = _mature(conn, raw)  # this refunds automatically if past deadline
        if job["requester_id"] != me["id"]:
            raise HTTPException(403, "only the requester can reclaim")
        if job["status"] == "refunded":
            return {"job_id": job_id, "status": "refunded", "reason": job["reason"]}
        raise HTTPException(409, f"job is {job['status']}; not reclaimable yet")


@app.post("/maintenance/sweep")
def sweep():
    """Process every due timeout. Safe to call on a timer; idempotent."""
    settled, refunded = [], []
    with db() as conn:
        open_jobs = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('escrowed','delivered')"
        ).fetchall()
        for j in open_jobs:
            after = _mature(conn, j)
            if after["status"] == "settled" and j["status"] != "settled":
                settled.append(after["id"])
            elif after["status"] == "refunded" and j["status"] != "refunded":
                refunded.append(after["id"])
    return {"settled": settled, "refunded": refunded}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, me=Depends(current_agent)):
    with db() as conn:
        raw = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if raw is None:
            raise HTTPException(404, "job not found")
        job = _mature(conn, raw)
        if me["id"] not in (job["requester_id"], job["provider_id"]):
            raise HTTPException(403, "not a party to this job")
        if job.get("verification"):
            job["verification"] = _json.loads(job["verification"])
    return job


# --------------------------------------------------------------------------
# public, no-auth, read-only — safe to expose; no keys, no job inputs/results
# --------------------------------------------------------------------------
@app.get("/stats")
def stats():
    with db() as conn:
        a = conn.execute("SELECT COUNT(*) n FROM agents").fetchone()["n"]
        s = conn.execute("SELECT COUNT(*) n FROM services WHERE active=1").fetchone()["n"]
        jrows = conn.execute("SELECT status, amount FROM jobs").fetchall()
        settled = [j for j in jrows if j["status"] == "settled"]
        open_escrow = conn.execute(
            "SELECT COALESCE(SUM(escrowed),0) e FROM agents"
        ).fetchone()["e"]
    return {
        "agents": a,
        "services": s,
        "jobs_total": len(jrows),
        "jobs_settled": len(settled),
        "volume_settled": sum(j["amount"] for j in settled),
        "open_escrow": open_escrow,
    }


@app.get("/activity")
def activity(limit: int = 25):
    limit = max(1, min(limit, 100))
    with db() as conn:
        rows = conn.execute(
            "SELECT j.id, j.status, j.reason, j.amount, j.auto, j.updated_at, "
            "s.name AS service, p.name AS provider, r.name AS requester "
            "FROM jobs j "
            "JOIN services s ON s.id = j.service_id "
            "JOIN agents p ON p.id = j.provider_id "
            "JOIN agents r ON r.id = j.requester_id "
            "ORDER BY j.updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"activity": [dict(r) for r in rows]}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    with open("dashboard.html", "r") as f:
        return f.read()


@app.get("/")
def root():
    return {"service": "Agent Clearinghouse", "version": app.version,
            "status": "ok", "dashboard": "/dashboard"}


init_db()
