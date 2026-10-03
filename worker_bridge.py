# TG Bot Hoster distributed worker bridge
import os, time, secrets, hashlib, json, threading
from datetime import datetime, timezone

COL_WORKERS = 'bridge_workers'
COL_JOBS = 'bridge_jobs'
COL_TOKENS = 'bridge_tokens'
COL_ASSIGNMENTS = 'bridge_assignments'

_lock = threading.Lock()


def _mongo():
    uri = os.getenv('MONGO_URI') or os.getenv('MONGO_DB_URI')
    if not uri:
        return None
    try:
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000)
        client.admin.command('ping')
        return client[os.getenv('DB_NAME', 'TG_HOSTER')]
    except Exception:
        return None


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(label='worker'):
    raw = 'wkr_' + secrets.token_urlsafe(32)
    return raw, token_hash(raw)


def register_worker(worker_id, label, token, capabilities=None, metadata=None):
    now = datetime.now(timezone.utc)
    doc = {'worker_id': worker_id, 'label': label or worker_id, 'token_hash': token_hash(token),
           'status': 'online', 'capabilities': capabilities or {}, 'metadata': metadata or {},
           'registered_at': now, 'last_heartbeat': now, 'current_jobs': 0}
    db = _mongo()
    if db is not None:
        db[COL_WORKERS].update_one({'worker_id': worker_id}, {'$set': doc, '$setOnInsert': {'created_at': now}}, upsert=True)
        return doc
    return doc


def authenticate_worker(token):
    if not token:
        return None
    h = token_hash(token)
    db = _mongo()
    if db is not None:
        return db[COL_WORKERS].find_one({'token_hash': h, 'status': {'$ne': 'revoked'}})
    return None


def heartbeat(worker_id, stats=None):
    now = datetime.now(timezone.utc)
    db = _mongo()
    if db is not None:
        db[COL_WORKERS].update_one({'worker_id': worker_id, 'status': {'$ne': 'revoked'}}, {'$set': {'status': 'online', 'last_heartbeat': now, 'stats': stats or {}, 'current_jobs': int((stats or {}).get('active_jobs', 0) or 0)}})
    return True


def list_workers():
    db = _mongo()
    if db is None:
        return []
    out=[]
    for d in db[COL_WORKERS].find({}, {'token_hash': 0}):
        d['_id'] = str(d.get('_id'))
        for k in ('registered_at','last_heartbeat','created_at'):
            if hasattr(d.get(k), 'isoformat'): d[k] = d[k].isoformat()
        out.append(d)
    return out


def revoke_worker(worker_id):
    db = _mongo()
    if db is None: return False
    r = db[COL_WORKERS].update_one({'worker_id': worker_id}, {'$set': {'status': 'revoked', 'revoked_at': datetime.now(timezone.utc)}})
    if not r.matched_count: return False
    worker = db[COL_WORKERS].find_one({'worker_id': worker_id}, {'token_hash':1})
    if worker and worker.get('token_hash'):
        db[COL_TOKENS].update_one({'token_hash': worker['token_hash']}, {'$set': {'revoked': True, 'revoked_at': datetime.now(timezone.utc)}})
    return True


def enqueue(worker_id, action, project):
    job_id = 'job_' + secrets.token_urlsafe(12)
    now = datetime.now(timezone.utc)
    doc = {'job_id': job_id, 'worker_id': worker_id, 'action': action, 'project': project,
           'status': 'queued', 'created_at': now, 'updated_at': now}
    db = _mongo()
    if db is None: raise RuntimeError('MongoDB is required for distributed workers')
    if action == 'start':
        db[COL_ASSIGNMENTS].update_one({'project_key': project.get('project_key')}, {'$set': {'project_key': project.get('project_key'), 'worker_id': worker_id, 'desired_state': 'running', 'updated_at': now}}, upsert=True)
    elif action == 'stop':
        db[COL_ASSIGNMENTS].update_one({'project_key': project.get('project_key')}, {'$set': {'project_key': project.get('project_key'), 'desired_state': 'stopped', 'updated_at': now}})
    db[COL_JOBS].insert_one(doc)
    return job_id


def claim_job(worker_id):
    db = _mongo()
    if db is None: return None
    recover_stale_jobs()
    from pymongo import ReturnDocument
    now = datetime.now(timezone.utc)
    doc = db[COL_JOBS].find_one_and_update(
        {'status': 'queued', '$or': [{'worker_id': worker_id}, {'worker_id': None}]},
        {'$set': {'status': 'running', 'worker_id': worker_id, 'started_at': now, 'updated_at': now}},
        sort=[('created_at', 1)], return_document=ReturnDocument.AFTER)
    if doc:
        doc['_id'] = str(doc['_id'])
        project = doc.get('project') or {}
        if project.get('project_key'):
            db[COL_ASSIGNMENTS].update_one({'project_key': project['project_key']}, {'$set': {'project_key': project['project_key'], 'worker_id': worker_id, 'updated_at': now}}, upsert=True)
    return doc


def finish_job(job_id, worker_id, status, result=None):
    db = _mongo()
    if db is None: return False
    allowed = {'running','completed','failed','queued'}
    if status not in allowed: return False
    now = datetime.now(timezone.utc)
    r = db[COL_JOBS].update_one({'job_id': job_id, 'worker_id': worker_id, 'status': {'$in': ['queued','running']}}, {'$set': {'status': status, 'result': result or {}, 'updated_at': now}})
    if not r.matched_count: return False
    if status in ('completed','failed'):
        db[COL_WORKERS].update_one({'worker_id': worker_id}, {'$inc': {'current_jobs': -1}})
    elif status == 'running':
        db[COL_WORKERS].update_one({'worker_id': worker_id}, {'$inc': {'current_jobs': 1}})
    return True


def get_assignment(project_key):
    db = _mongo()
    if db is None: return None
    return db[COL_ASSIGNMENTS].find_one({'project_key': project_key})

def recover_stale_jobs(max_age_seconds=120):
    db = _mongo()
    if db is None: return 0
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
    r = db[COL_JOBS].update_many({'status':'running','updated_at':{'$lt':cutoff}}, {'$set':{'status':'queued','worker_id':None,'recovered_at':datetime.now(timezone.utc),'updated_at':datetime.now(timezone.utc)}})
    return r.modified_count

def list_jobs(limit=100):
    db = _mongo()
    if db is None: return []
    out=[]
    for d in db[COL_JOBS].find({}).sort('created_at', -1).limit(int(limit)):
        d['_id']=str(d['_id'])
        for k in ('created_at','updated_at','started_at'):
            if hasattr(d.get(k), 'isoformat'): d[k]=d[k].isoformat()
        out.append(d)
    return out
