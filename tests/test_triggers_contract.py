"""Regression contract tests for the Triggers section (formerly Schedule).

Pins the BACKEND contracts the four new trigger views (Heartbeat / Scheduled /
Daemons / Webhooks) consume, written BEFORE the frontend teardown of
schedule.js. That refactor is intentionally frontend-only — the backend does
not change — so these tests prove the data shape the new views rely on and will
catch any accidental drift while we rebuild the UI.

Covers the gaps not already in test_trigger_system.py (which pins the scheduler
model: type defaults, daemon/webhook/heartbeat creation, migration, limits):
  - `source` survives create_task — the Scheduled tab's User|AI column split
    rides entirely on `source == "ai_scheduled"`.
  - /api/continuity/tasks ?type= and ?heartbeat= filters — each view fetches
    exactly its slice (Scheduled=?type=task, Daemons=?type=daemon, etc.).
  - merged-timeline shape — the Heartbeat/Scheduled per-tab timelines read
    now/past/future.
"""
import json
import threading
from unittest.mock import MagicMock


def _make_scheduler(tmp_path, tasks=None):
    """Real ContinuityScheduler with mocked system/executor.

    Mirrors test_trigger_system._make_scheduler so we exercise the actual
    load/create/list code paths, not a stub.
    """
    from core.continuity.scheduler import ContinuityScheduler

    base_dir = tmp_path / "user" / "continuity"
    base_dir.mkdir(parents=True)
    if tasks is not None:
        (base_dir / "tasks.json").write_text(
            json.dumps({"tasks": tasks}), encoding="utf-8"
        )

    sched = ContinuityScheduler.__new__(ContinuityScheduler)
    sched.system = MagicMock()
    sched.executor = MagicMock()
    sched._running = False
    sched._thread = None
    sched._lock = threading.Lock()
    sched._base_dir = base_dir
    sched._tasks_path = base_dir / "tasks.json"
    sched._activity_path = base_dir / "activity.json"
    sched._tasks = {}
    sched._activity = []
    sched._task_running = {}
    sched._task_pending = {}
    sched._task_last_matched = {}
    sched._task_progress = {}
    sched._event_threads = []
    sched._load_tasks()
    sched._load_activity()
    return sched


def _created_id(created):
    """create_task may return the task dict or the id — handle both."""
    return created["id"] if isinstance(created, dict) else created


# Seed covering all four trigger types + the user/AI source split.
SEED = [
    {"id": "u1", "name": "User task", "type": "task", "heartbeat": False,
     "source": "user", "schedule": "0 9 * * *", "enabled": True},
    {"id": "a1", "name": "AI task", "type": "task", "heartbeat": False,
     "source": "ai_scheduled", "schedule": "0 9 * * *", "enabled": True},
    {"id": "hb1", "name": "Beat", "type": "heartbeat", "heartbeat": True,
     "schedule": "*/15 * * * *", "enabled": True},
    {"id": "d1", "name": "Daemon", "type": "daemon", "heartbeat": False,
     "trigger_config": {"source": "discord_message"}, "enabled": True},
    {"id": "w1", "name": "Hook", "type": "webhook", "heartbeat": False,
     "trigger_config": {"path": "deploy"}, "enabled": True},
]


class TestSourceContract:
    """The Scheduled tab's User|AI split depends on `source` surviving create."""

    def test_ai_scheduled_source_preserved(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        created = sched.create_task(
            {"name": "AI", "type": "task", "schedule": "0 9 * * *",
             "source": "ai_scheduled"})
        fetched = sched.get_task(_created_id(created))
        assert fetched.get("source") == "ai_scheduled", \
            "AI column needs `source` to persist through create_task"

    def test_user_task_not_ai_scheduled(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        created = sched.create_task(
            {"name": "User", "type": "task", "schedule": "0 9 * * *"})
        fetched = sched.get_task(_created_id(created))
        assert fetched.get("source") != "ai_scheduled", \
            "a user-made task must land in the User column"

    def test_partition_by_source(self, tmp_path):
        sched = _make_scheduler(tmp_path, tasks=SEED)
        scheduled = [t for t in sched.list_tasks() if t.get("type", "task") == "task"]
        ai = {t["id"] for t in scheduled if t.get("source") == "ai_scheduled"}
        user = {t["id"] for t in scheduled if t.get("source") != "ai_scheduled"}
        assert ai == {"a1"}
        assert user == {"u1"}


class TestContinuityApiFilters:
    """Route-level ?type / ?heartbeat filters — each view's exact slice."""

    @staticmethod
    def _wire(mock_system):
        mock_system.continuity_scheduler.list_tasks.return_value = SEED

    def _ids(self, resp):
        assert resp.status_code == 200, resp.text
        return {t["id"] for t in resp.json()["tasks"]}

    def test_no_filter_returns_all(self, client, mock_system):
        self._wire(mock_system)
        c, _ = client
        assert self._ids(c.get("/api/continuity/tasks")) == {"u1", "a1", "hb1", "d1", "w1"}

    def test_type_task_is_scheduled_set(self, client, mock_system):
        self._wire(mock_system)
        c, _ = client
        # Scheduled view: both user + AI tasks, no heartbeats/daemons/webhooks
        assert self._ids(c.get("/api/continuity/tasks?type=task")) == {"u1", "a1"}

    def test_type_daemon(self, client, mock_system):
        self._wire(mock_system)
        c, _ = client
        assert self._ids(c.get("/api/continuity/tasks?type=daemon")) == {"d1"}

    def test_type_webhook(self, client, mock_system):
        self._wire(mock_system)
        c, _ = client
        assert self._ids(c.get("/api/continuity/tasks?type=webhook")) == {"w1"}

    def test_heartbeat_true_excludes_event_types(self, client, mock_system):
        self._wire(mock_system)
        c, _ = client
        # Heartbeat view: only heartbeats — daemons/webhooks must NOT leak in
        assert self._ids(c.get("/api/continuity/tasks?heartbeat=true")) == {"hb1"}

    def test_heartbeat_false_is_scheduled_only(self, client, mock_system):
        self._wire(mock_system)
        c, _ = client
        assert self._ids(c.get("/api/continuity/tasks?heartbeat=false")) == {"u1", "a1"}

    def test_merged_timeline_shape(self, client, mock_system):
        mock_system.continuity_scheduler.get_merged_timeline.return_value = {
            "now": None, "past": [], "future": []}
        c, _ = client
        r = c.get("/api/continuity/merged-timeline")
        assert r.status_code == 200, r.text
        assert set(r.json().keys()) >= {"now", "past", "future"}
