"""Serves research requests coming from the React app via FastAPI.

Can be run on its own (python local_bridge_agent.py) for a Cosmitude-free setup,
and is also used by bridge_agent.py, which adds the Cosmitude Bridge on top.
"""
import os
import sys
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv()

from research_pipeline import generate_research  # noqa: E402

API = os.getenv("RESEARCHDESK_API", "http://127.0.0.1:8000").rstrip("/")
BRIDGE_REQUEST_URL = f"{API}/bridge/request"
BRIDGE_RESULT_URL = f"{API}/bridge/result"
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))


def make_console_safe():
    """Windows consoles (cp1252) raise UnicodeEncodeError when printing non-Latin
    topics, which would kill the worker thread. Never let printing break us."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def get_research_request():
    r = requests.get(BRIDGE_REQUEST_URL, timeout=10)
    r.raise_for_status()
    return r.json()          # None when nothing is pending


def post_result(request_id, result, tries=3) -> bool:
    for attempt in range(tries):
        try:
            r = requests.post(BRIDGE_RESULT_URL,
                              json={"request_id": request_id, "result": result}, timeout=15)
            r.raise_for_status()
            print("Result sent to FastAPI:", r.json())
            return True
        except requests.RequestException as e:
            print(f"Could not post result (attempt {attempt + 1}/{tries}):", e)
            time.sleep(1)
    return False


def process_request(req, on_result=None):
    """Generate research for one React request and hand the result back.
    on_result(topic, result) is called afterwards (e.g. to mirror into Cosmitude);
    failures there never affect what the React app receives."""
    rid, topic = req["request_id"], req.get("input") or ""
    print("Received:", rid, topic)
    ok = True
    try:
        result = generate_research(topic)
    except Exception as e:                      # report it instead of timing out
        ok = False
        print("Research generation failed:", e)
        result = {"status": "error", "message": f"Research generation failed: {e}"}
    post_result(rid, result)
    if ok and on_result:
        try:
            on_result(topic, result)
        except Exception as e:
            print("Post-processing of result failed:", e)


def serve_react_requests(on_result=None):
    """Poll FastAPI forever; every request runs in its own thread so a slow
    Gemini call never stops polling (FastAPI treats a silent agent as down)."""
    print(f"Polling {BRIDGE_REQUEST_URL} for ResearchDesk requests")
    api_down = False
    while True:
        try:
            req = get_research_request()
            if api_down:
                print("FastAPI reachable again")
                api_down = False
        except (requests.RequestException, ValueError) as e:
            if not api_down:
                print("Could not reach FastAPI:", e)
                api_down = True
            time.sleep(max(POLL_SECONDS, 2))
            continue
        if req:
            threading.Thread(target=process_request, args=(req, on_result),
                             daemon=True).start()
            continue                             # check for more immediately
        time.sleep(POLL_SECONDS)


def main():
    make_console_safe()
    print("[local] research agent (no Cosmitude)")
    serve_react_requests()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
