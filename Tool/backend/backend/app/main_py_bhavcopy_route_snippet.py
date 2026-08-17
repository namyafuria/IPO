# --- Paste into main.py, next to the existing POST /api/sync/gmp route ---
#
# main.py wasn't uploaded this session, so this is written as a standalone
# snippet rather than an in-place edit. It assumes the same _sync_lock
# object and pattern /api/sync/gmp already uses (per item 4: "guarded by
# the existing _sync_lock so it can't collide with the other sync
# routes") -- adjust the import/lock-usage lines below to match exactly
# how /api/sync/gmp does it in your real main.py.

from .scheduler import sync_bhavcopy  # add alongside the existing scheduler imports


@app.post("/api/sync/bhavcopy")
def trigger_bhavcopy_sync():
    """Manually triggers the daily bhavcopy price sync + gap-fill fallback
    (see bhavcopy_sync.py). Same _sync_lock guard as /api/sync/gmp so this
    can't run concurrently with another sync route."""
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A sync is already in progress.")
    try:
        sync_bhavcopy()
        return {"status": "ok"}
    finally:
        _sync_lock.release()
