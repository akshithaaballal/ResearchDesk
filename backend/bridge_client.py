import os
import threading
import time
import uuid

from dotenv import load_dotenv

# Must run BEFORE BridgeClient() reads BRIDGE_MODE below. (main.py used to import
# this module first and call load_dotenv() afterwards, so .env was ignored here.)
load_dotenv()


class BridgeNotAvailable(Exception):
    pass


class BridgeClient:
    """In-memory request/result hand-off between the FastAPI /research endpoint
    and the bridge agent (bridge_agent.py / local_bridge_agent.py)."""

    def __init__(self):
        self.mode = os.getenv("BRIDGE_MODE", "local").strip().lower()
        self._lock = threading.Lock()
        self._pending = {}      # request_id -> {"request_id", "input", "claimed"}
        self._events = {}       # request_id -> threading.Event
        self._results = {}      # request_id -> result dict
        self._order = []        # request_ids, oldest first
        self.last_agent_poll = None       # epoch seconds, set whenever an agent polls
        self.last_cosmitude_sync = None   # epoch seconds, set by agent heartbeat
        self.last_cosmitude_items = None
        self.last_unsolicited_result = None

    @property
    def transport(self) -> str:
        return "cosmitude" if self.mode == "cosmitude" else "local-simulation"

    # ---- FastAPI side -------------------------------------------------
    def submit(self, research_input: str) -> str:
        request_id = uuid.uuid4().hex
        with self._lock:
            self._pending[request_id] = {
                "request_id": request_id, "input": research_input, "claimed": False,
            }
            self._events[request_id] = threading.Event()
            self._order.append(request_id)
        return request_id

    def agent_alive(self, max_age: float) -> bool:
        t = self.last_agent_poll
        return t is not None and (time.time() - t) <= max_age

    def wait_for_result(self, request_id: str, timeout: float):
        ev = self._events.get(request_id)
        if ev and ev.wait(timeout):
            with self._lock:
                self._events.pop(request_id, None)
                return self._results.pop(request_id, None)
        return None

    def discard(self, request_id: str):
        with self._lock:
            self._pending.pop(request_id, None)
            self._events.pop(request_id, None)
            self._results.pop(request_id, None)
            if request_id in self._order:
                self._order.remove(request_id)

    # ---- Agent side ---------------------------------------------------
    def claim_next(self):
        """Hand the oldest unclaimed request to an agent exactly once."""
        self.last_agent_poll = time.time()
        with self._lock:
            for rid in self._order:
                req = self._pending.get(rid)
                if req and not req["claimed"]:
                    req["claimed"] = True
                    return {"request_id": rid, "input": req["input"]}
        return None

    def complete(self, request_id, result: dict) -> bool:
        with self._lock:
            if request_id and request_id in self._events:
                self._results[request_id] = result
                self._pending.pop(request_id, None)
                if request_id in self._order:
                    self._order.remove(request_id)
                self._events[request_id].set()
                return True
            self.last_unsolicited_result = result
            return False

    def cosmitude_heartbeat(self, items=None):
        self.last_cosmitude_sync = time.time()
        self.last_cosmitude_items = items

    def status(self) -> dict:
        now = time.time()

        def age(t):
            return None if t is None else round(now - t, 1)

        cos_age = age(self.last_cosmitude_sync)
        return {
            "mode": self.mode,
            "transport": self.transport,
            "pending_requests": len(self._order),
            "agent_last_poll_seconds_ago": age(self.last_agent_poll),
            "agent_seen": self.last_agent_poll is not None,
            "cosmitude_agent_last_sync_seconds_ago": cos_age,
            "cosmitude_agent_seen": cos_age is not None,
            "request_path_uses_cosmitude": self.mode == "cosmitude",
        }


bridge = BridgeClient()
