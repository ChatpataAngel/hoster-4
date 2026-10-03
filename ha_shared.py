"""Shared HA state/files for TG Bot Hoster.

Only the current Mongo lease holder writes snapshots. Standbys restore the newest
snapshot before loading the local runtime state. This makes failover stateful
instead of merely moving Telegram polling between machines.
"""
from __future__ import annotations
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone

_lock = threading.Lock()
_last_sync = 0.0
_last_restore = 0.0
_last_signature = None


def enabled(ha):
    return bool(ha and ha.ha_enabled() and ha.mongo_uri())


def _db(ha):
    return ha._mongo()[ha.db_name()]


def _bucket(ha):
    import gridfs
    return gridfs.GridFSBucket(_db(ha), bucket_name="tg_hoster_files")


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:180]


def _latest(ha, kind):
    return _db(ha).shared_snapshots.find_one({"kind": kind}, sort=[("updated_at", -1)])


def _put_blob(ha, kind, source_path, metadata=None):
    bucket = _bucket(ha)
    size = os.path.getsize(source_path)
    with open(source_path, "rb") as f:
        file_id = bucket.upload_from_stream(
            _safe_name(f"{kind}-{int(time.time())}"), f,
            metadata={"kind": kind, **(metadata or {})}
        )
    now = datetime.now(timezone.utc)
    _db(ha).shared_snapshots.insert_one({
        "kind": kind, "file_id": file_id, "size": size, "updated_at": now,
        "instance_id": ha.instance_id(),
    })
    # Keep the newest two generations; delete old GridFS blobs too.
    old_docs = list(_db(ha).shared_snapshots.find({"kind": kind}).sort("updated_at", -1).skip(2))
    for doc in old_docs:
        try: bucket.delete(doc["file_id"])
        except Exception: pass
        _db(ha).shared_snapshots.delete_one({"_id": doc["_id"]})


def _snapshot_sqlite(database_path):
    fd, path = tempfile.mkstemp(prefix="tg_hoster_db_", suffix=".sqlite")
    os.close(fd)
    src = sqlite3.connect(database_path, check_same_thread=False)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close(); src.close()
    return path


def _snapshot_dir(root, max_mb=200):
    fd, path = tempfile.mkstemp(prefix="tg_hoster_files_", suffix=".zip")
    os.close(fd)
    total = 0
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            if os.path.isdir(root):
                base = os.path.abspath(root)
                for current, dirs, files in os.walk(root):
                    dirs[:] = [d for d in dirs if d not in {'.venv', 'node_modules', '__pycache__'}]
                    for name in files:
                        src = os.path.join(current, name)
                        try:
                            if name.endswith('.log') or name == '.DS_Store':
                                continue
                            size = os.path.getsize(src)
                            total += size
                            if total > max_mb * 1024 * 1024:
                                raise ValueError(f"shared artifact limit {max_mb}MB exceeded")
                            arc = os.path.relpath(src, base).replace(os.sep, "/")
                            z.write(src, arc)
                        except OSError:
                            continue
        return path
    except Exception:
        try: os.remove(path)
        except Exception: pass
        raise


def sync_once(ha, database_path, upload_dir, pending_dir, max_mb=200):
    """Leader-only snapshot. Returns a small status dict."""
    global _last_sync
    if not enabled(ha): return {"ok": False, "reason": "disabled"}
    if not ha.get_state().get("is_leader"): return {"ok": False, "reason": "not_leader"}
    global _last_signature
    with _lock:
        now = time.time()
        if now - _last_sync < 30:
            return {"ok": True, "skipped": True}
        _last_sync = now
    def tree_signature(root):
        items = []
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {'.venv', 'node_modules', '__pycache__'}]
            for name in files:
                if name.endswith('.log'): continue
                path = os.path.join(current, name)
                try:
                    st = os.stat(path)
                    items.append((os.path.relpath(path, root), st.st_size, st.st_mtime_ns))
                except OSError: pass
        return hash(tuple(sorted(items)))
    signature = (os.path.getmtime(database_path) if os.path.exists(database_path) else 0,
                 tree_signature(upload_dir), tree_signature(pending_dir))
    if signature == _last_signature:
        return {"ok": True, "skipped": True, "reason": "unchanged"}
    _last_signature = signature
    db_tmp = None; files_tmp = None
    try:
        db_tmp = _snapshot_sqlite(database_path)
        _put_blob(ha, "database", db_tmp)
        files_tmp = _snapshot_dir(upload_dir, max_mb=max_mb)
        _put_blob(ha, "projects", files_tmp)
        pending_tmp = _snapshot_dir(pending_dir, max_mb=max_mb)
        try: _put_blob(ha, "pending", pending_tmp)
        finally:
            try: os.remove(pending_tmp)
            except Exception: pass
        return {"ok": True, "updated_at": now}
    finally:
        for p in (db_tmp, files_tmp):
            if p:
                try: os.remove(p)
                except Exception: pass


def _restore_blob(ha, kind, destination, is_dir=False):
    doc = _latest(ha, kind)
    if not doc: return False
    bucket = _bucket(ha)
    fd, tmp = tempfile.mkstemp(prefix="tg_hoster_restore_", suffix=".bin")
    os.close(fd)
    try:
        with open(tmp, "wb") as out:
            bucket.download_to_stream(doc["file_id"], out)
        if is_dir:
            os.makedirs(destination, exist_ok=True)
            # The snapshot is authoritative; remove stale local artifacts first.
            for name in os.listdir(destination):
                path = os.path.join(destination, name)
                try:
                    if os.path.isdir(path) and not os.path.islink(path): shutil.rmtree(path)
                    else: os.remove(path)
                except Exception: pass
            with zipfile.ZipFile(tmp, "r") as z:
                base = os.path.abspath(destination)
                for info in z.infolist():
                    target = os.path.abspath(os.path.join(base, info.filename))
                    if os.path.commonpath([base, target]) != base:
                        raise ValueError("unsafe shared archive path")
                z.extractall(destination)
        else:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(tmp, destination)
        return True
    finally:
        try: os.remove(tmp)
        except Exception: pass


def restore_latest(ha, database_path, upload_dir, pending_dir):
    """Restore only when a newer shared snapshot exists than the local files."""
    global _last_restore
    if not enabled(ha): return {"ok": False, "reason": "disabled"}
    with _lock:
        if time.time() - _last_restore < 20:
            return {"ok": True, "skipped": True}
        _last_restore = time.time()
    result = {}
    try:
        db = _latest(ha, "database")
        if db:
            local_mtime = os.path.getmtime(database_path) if os.path.exists(database_path) else 0
            shared_ts = db["updated_at"].timestamp()
            if shared_ts > local_mtime + 2:
                result["database"] = _restore_blob(ha, "database", database_path)
        def dir_mtime(root):
            newest = 0
            if os.path.isdir(root):
                for current, dirs, files in os.walk(root):
                    dirs[:] = [d for d in dirs if d not in {'.venv', 'node_modules', '__pycache__'}]
                    for name in files:
                        try: newest = max(newest, os.path.getmtime(os.path.join(current, name)))
                        except OSError: pass
            return newest
        for kind, root, key in (("projects", upload_dir, "projects"), ("pending", pending_dir, "pending")):
            doc = _latest(ha, kind)
            if doc and doc["updated_at"].timestamp() > dir_mtime(root) + 2:
                result[key] = _restore_blob(ha, kind, root, is_dir=True)
            else:
                result[key] = False
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
