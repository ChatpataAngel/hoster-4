"""MongoDB leader-lease for the TG Bot Hoster (telebot version).

Same idea as 123/src/utils/lease.py but threading-based (telebot is sync):
- All instances share ONE MongoDB doc: <DB>.instance_leases {_id:"poller"}.
- Every LEASE_POLL_SECONDS each instance runs try_acquire() — an atomic
  find_one_and_update that only wins if holder==me OR lease expired.
- Only the winner polls Telegram (telebot.infinity_polling). Standbys keep
  Flask alive so Render health checks + Vercel dashboard still see them.
- Mongo down -> return None -> KEEP current role (no split-brain).

Set MONGO_URI empty + HA_ENABLED=0 for legacy single-process behaviour.
"""
import os
import time
import threading
import logging

log = logging.getLogger("hoster.ha")

LEASE_COLLECTION = "instance_leases"
LEASE_ID = "poller"

_state = {
    "is_leader": False,
    "polling": False,
    "since": None,        # iso timestamp when became leader
    "expires_at": None,   # unix ts when our lease expires
    "last_attempt": None,
    "last_error": "",
}
_lock = threading.Lock()
_client = None


def _cfg():
    ttl = int(os.getenv("LEASE_TTL_SECONDS", "45") or 45)
    ttl = max(15, min(300, ttl))
    poll = float(os.getenv("LEASE_POLL_SECONDS", "5") or 5)
    poll = max(2.0, min(60.0, poll))
    return ttl, poll


def ha_enabled():
    return os.getenv("HA_ENABLED", "0").lower() in ("1", "true", "yes", "on")


def instance_id():
    return os.getenv("INSTANCE_ID", "") or "local-%d" % os.getpid()


def mongo_uri():
    return os.getenv("MONGO_URI", "") or os.getenv("MONGO_DB_URI", "")


def db_name():
    return os.getenv("DB_NAME", "TG_HOSTER") or "TG_HOSTER"


def _mongo():

    global _client
    if _client is None:
        from pymongo import MongoClient
        _client = MongoClient(
            mongo_uri(), serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000, socketTimeoutMS=10000,
        )
    return _client


def get_state():
    with _lock:
        d = dict(_state)
    exp_in = None
    if d["is_leader"] and d["expires_at"]:
        exp_in = round(d["expires_at"] - time.time(), 1)
    d["lease_expires_in"] = exp_in
    d["instance_id"] = instance_id()
    d["ha_enabled"] = ha_enabled()
    return d


def try_acquire():
    """True=mine, False=someone else holds it. Raises on Mongo failure."""
    from datetime import datetime, timezone, timedelta
    ttl, _ = _cfg()
    me = instance_id()
    now = datetime.now(timezone.utc)
    col = _mongo()[db_name()][LEASE_COLLECTION]
    try:
        col.update_one(
            {"_id": LEASE_ID},
            {"$setOnInsert": {
                "holder": "",
                "expires_at": now - timedelta(seconds=1),
                "created_at": now,
            }},
            upsert=True,
        )
    except Exception:
        pass  # lost insert race — fine, fall through to atomic claim
    from pymongo import ReturnDocument
    doc = col.find_one_and_update(
        {"_id": LEASE_ID, "$or": [
            {"holder": me},
            {"expires_at": {"$lte": now}},
        ]},
        {"$set": {
            "holder": me,
            "expires_at": now + timedelta(seconds=ttl),
            "renewed_at": now,
        }},
        return_document=ReturnDocument.AFTER,
    )
    mine = bool(doc and doc.get("holder") == me)
    with _lock:
        _state["last_attempt"] = now.isoformat()
        if mine:
            if not _state["is_leader"]:
                log.info("Instance %s acquired poller lease — becoming LEADER", me)
                _state["since"] = now.isoformat()
            _state["is_leader"] = True
            _state["expires_at"] = (now + timedelta(seconds=ttl)).timestamp()
            _state["last_error"] = ""
        else:
            if _state["is_leader"]:
                log.warning("Instance %s LOST poller lease — stepping down", me)
            _state["is_leader"] = False
            _state["since"] = None
            _state["expires_at"] = None
    return mine


def release():
    """Expire our own lease so a standby promotes immediately."""
    me = instance_id()
    with _lock:
        if not _state["is_leader"]:
            return
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        _mongo()[db_name()][LEASE_COLLECTION].update_one(
            {"_id": LEASE_ID, "holder": me},
            {"$set": {"expires_at": now, "released_at": now}},
        )
        log.info("Instance %s released poller lease", me)
    except Exception as e:
        log.warning("Could not release lease: %s", e)
    finally:
        with _lock:
            _state["is_leader"] = False
            _state["polling"] = False
            _state["since"] = None
            _state["expires_at"] = None


def set_polling(v):
    with _lock:
        _state["polling"] = bool(v)


def set_error(text):
    with _lock:
        _state["last_error"] = str(text)[:300]
