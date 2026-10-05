"""ResearchDesk bridge agent: Cosmitude Bridge <-> Gemini <-> ResearchDesk API.

One process does everything (run it from the backend folder: python bridge_agent.py):

  1. React app -> FastAPI /research -> this agent (polls FastAPI) -> Gemini ->
     result back to FastAPI -> React.  Works even if Cosmitude is unreachable.
     With BRIDGE_MODE=cosmitude the topic and the finished result card are also
     posted into the Cosmitude forum (best effort, never blocks the React reply).

  2. Cosmitude forum users -> ResearchInput flow -> this agent (syncUserData) ->
     Gemini -> result card sent back into the forum with sendBridge, and also
     pushed to FastAPI.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

from local_bridge_agent import make_console_safe, serve_react_requests  # noqa: E402
from research_pipeline import generate_research  # noqa: E402

BASE_URL = os.getenv("COSMITUDE_BASE_URL", "https://ca.cosmitude.com/").strip()
if not BASE_URL.endswith("/"):
    BASE_URL += "/"
SERVER_ID = os.getenv(
    "COSMITUDE_SERVER_ID",
    "c9b6722d-5dbf-4b4f-a28e-692b4d26c1cf-7870eb8e-2f45-458a-9a70-d6b2d71ee871",
)
forumID = os.getenv("COSMITUDE_FORUM_ID", "3658ff1d-f77d-4118-98e2-8b4a785703d1")
USERNAME = os.getenv("COSMITUDE_USERNAME", "")
PASSWORD = os.getenv("COSMITUDE_PASSWORD", "")
if not PASSWORD and os.getenv("COSMITUDE_PASSWORD_B64"):
    PASSWORD = base64.b64decode(os.environ["COSMITUDE_PASSWORD_B64"]).decode("utf-8")

# Flow that carries research requests (admin flow in the forum template)
RESEARCH_FLOW_ID = os.getenv("COSMITUDE_RESEARCH_FLOW_ID", "ResearchDeskAI_ResearchInput")
# Flow the finished research card is sent to
REPLY_FLOW_ID = os.getenv("COSMITUDE_REPLY_FLOW_ID", "ResearchDeskAI_Resultspage/card")
# Optional explicit key (dotted path) holding the topic text; auto-detected if unset/not found
INPUT_KEY = os.getenv("COSMITUDE_INPUT_KEY", "")
BRIDGE_MODE = os.getenv("BRIDGE_MODE", "local").strip().lower()
RESEARCHDESK_API = os.getenv("RESEARCHDESK_API", "http://127.0.0.1:8000").rstrip("/")
SYNC_SECONDS = float(os.getenv("COSMITUDE_SYNC_SECONDS", "10"))

# Next to this script, so it works regardless of the current working directory.
TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "ResearchDeskAI_ResearchDeskAI_Admin.txt")

Sessionid = ""
MobileNumber = ""
_seen = set()                 # hashes of bridge messages already handled
_own_topics = {}              # sha256(topic) -> time we mirrored it ourselves
_lock = threading.Lock()


def _log(*a):
    print("[cosmitude]", *a, flush=True)


# --------------------------------------------------------------------------
# HTTP + login + forum template
# --------------------------------------------------------------------------
def _post(endpoint, payload):
    r = requests.post(BASE_URL + endpoint, data=json.dumps(payload),
                      headers={"Content-Type": "application/json"}, timeout=60)
    r.raise_for_status()
    return r.json()


def login():
    global Sessionid, MobileNumber

    if not USERNAME or not PASSWORD:
        raise RuntimeError("Set COSMITUDE_USERNAME and COSMITUDE_PASSWORD in .env")

    resp = _post("loginForDevice", {
        "MACAddress": "Bridge-Web",
        "UserName": USERNAME,
        "Password": PASSWORD,
        "ServerID": SERVER_ID,
    })
    msg = resp.get("ErrorMessage")
    if not isinstance(msg, dict) or "SessionID" not in msg:
        raise RuntimeError(f"Cosmitude login failed: ErrorCode={resp.get('ErrorCode')} "
                           f"ErrorMessage={msg!r}")
    Sessionid = msg["SessionID"]
    MobileNumber = msg["CountryCode"] + msg["MobileNumber"]
    _log("login OK")

    try:
        getForumDetails()
    except Exception as e:                      # template is refreshed again on sync
        _log("could not fetch forum template:", e)


def getForumDetails():
    resp = _post("getRequestedForumDetails", {
        "MACAddress": "Bridge-Web", "ForumID": forumID,
        "SessionID": Sessionid, "ServerID": SERVER_ID,
    })
    if resp.get("ErrorCode") == 1172:
        for singleForum in resp["ErrorMessage"]["ForumDataArray"]:
            details = singleForum[forumID]["NewForumJsonData"]
            _save_template(details)
        _log("forum template saved")


def _save_template(tpl):
    with open(TEMPLATE_FILE, "w", encoding="utf-8") as f:
        json.dump(tpl, f)


def _load_template():
    if not os.path.exists(TEMPLATE_FILE) and Sessionid:
        getForumDetails()
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _flow_info(flow):
    """-> (FID, {lower-case label name: id of the field it describes}) for a flow,
    looked up in the forum template's AdminFlow, then UserFlow."""
    tpl = _load_template()
    for section_name in ("AdminFlow", "UserFlow"):
        section = tpl.get(section_name) or {}
        entry = section.get(flow)
        if isinstance(entry, dict) and "FID" in entry:
            fields = {}
            for fmt_name in entry.get("Formats") or []:
                fmt = section.get(fmt_name)
                if not isinstance(fmt, dict):
                    continue
                for row in fmt.values():
                    for cell in (row.values() if isinstance(row, dict) else []):
                        if isinstance(cell, dict) and cell.get("type") == "LABEL":
                            for assoc in cell.get("Associated") or []:
                                fields.setdefault(
                                    str(cell.get("Name", "")).strip().lower(), assoc["ID"])
            return entry["FID"], fields, tpl
    raise KeyError(f"Flow {flow!r} not found in forum template (AdminFlow/UserFlow)")


# --------------------------------------------------------------------------
# Sending into the forum (this is Cosmitude's generated sendBridge)
# --------------------------------------------------------------------------
def _b64(text):
    return base64.b64encode(str(text).encode("utf-8")).decode("utf-8")


def send_bridge(replyFlow, replyData, bridgeData=None) -> bool:
    """POST one message into a forum flow. Never raises; returns True on HTTP 200."""
    try:
        FID, _, tpl = _flow_info(replyFlow)
        if not Sessionid:
            raise RuntimeError("not logged in to Cosmitude yet")
        bridgeData = bridgeData or {}
        tempJson = {
            "ForumID": bridgeData.get("ForumID", forumID),
            "SessionID": Sessionid,
            "MACAddress": "Bridge-Web",
            "Time": "6516516156161",
            "ScheduledDateTime": "now",
            "ScheduledBoolean": 0,
            "FlowID": replyFlow,
            "EnableChat": 1,
            "FlowType": "Custom",
            "BridgeForward": 0,
            "TemplateID": bridgeData.get("TemplateID", tpl.get("TemplateID", "")),
            "TextCount": 1,
            "ImageCount": 1,
            "InvoiceID": "",
            "DocumentCount": 0,
            "User": False,
            "VideoCount": 0,
            "ReplyBridgeID": "",
            "HiddenFlow": False,
            "TempBridgeId": str(uuid.uuid4()),
            replyFlow: replyData,
            "FID": FID,
            "ServerID": SERVER_ID,
            "SentTo": "0",
        }
        response = requests.post(BASE_URL + "bridgeSendingToForum", files={},
                                 data={"Data": json.dumps(tempJson)}, timeout=60)
        body = response.text[:200].replace("\n", " ")
        _log(f"sendBridge -> {replyFlow}: HTTP {response.status_code} {body}")
        return response.status_code == 200
    except Exception as e:
        _log(f"sendBridge to {replyFlow} failed:", e)
        return False


def _list_text(items):
    return "\n".join(f"- {i}" for i in items)


def send_result_card(result, bridgeData=None) -> bool:
    """Send the research result into the results flow, filling each field by its
    label in the forum template (Brief Title, Overview, Key Concepts, ...)."""
    _, fields, _ = _flow_info(REPLY_FLOW_ID)
    values = {
        "brief title": result.get("title", ""),
        "title": result.get("title", ""),
        "overview": result.get("overview", ""),
        "key concepts": _list_text(result.get("key_concepts", [])),
        "key findings": _list_text(result.get("key_findings", [])),
        "exploration questions": _list_text(result.get("exploration_questions", [])),
    }
    reply = {fid: _b64(values[label]) for label, fid in fields.items()
             if label in values and values[label]}
    if not reply:                               # template has no recognisable labels
        first = next(iter(fields.values()), None)
        if first is None:
            _log(f"flow {REPLY_FLOW_ID!r} has no input fields in the template")
            return False
        reply = {first: _b64(json.dumps(result, ensure_ascii=False))}
    return send_bridge(REPLY_FLOW_ID, reply, bridgeData)


# --------------------------------------------------------------------------
# Reading incoming messages
# --------------------------------------------------------------------------
def _dig(data, dotted):
    for part in dotted.split("."):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return None
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def _maybe_b64(value):
    """Cosmitude carries field values base64-encoded; plain text passes through."""
    s = value.strip()
    if len(s) >= 4 and len(s) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", s):
        try:
            dec = base64.b64decode(s, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return value
        if dec.strip() and all(ch.isprintable() or ch in "\r\n\t" for ch in dec):
            return dec
    return value


def extract_topic(bridge_data, flow):
    candidates = []
    if INPUT_KEY:
        candidates.append(_dig(bridge_data, INPUT_KEY))
    try:
        _, fields, _ = _flow_info(flow)
    except Exception:
        fields = {}
    ids = list(fields.values())
    payload = bridge_data.get(flow)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    if isinstance(payload, dict):
        candidates += [payload.get(i) for i in ids]
    candidates += [_find_key(bridge_data, i) for i in ids]
    if isinstance(payload, dict):               # last resort: any text in the flow payload
        candidates += list(payload.values())
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return _maybe_b64(c).strip()
    return None


def _remember_own_topic(topic):
    h = hashlib.sha256(topic.strip().encode("utf-8")).hexdigest()
    with _lock:
        now = time.time()
        for k in [k for k, t in _own_topics.items() if now - t > 600]:
            del _own_topics[k]
        _own_topics[h] = now


def _is_own_topic(topic):
    h = hashlib.sha256(topic.strip().encode("utf-8")).hexdigest()
    with _lock:
        return h in _own_topics and time.time() - _own_topics[h] <= 600


def _post_unsolicited(result):
    try:   # visible to ResearchDesk (no React request id)
        requests.post(f"{RESEARCHDESK_API}/bridge/result",
                      json={"request_id": None, "result": result}, timeout=10)
    except requests.RequestException as e:
        _log("FastAPI not reachable:", e)


def _process_forum_topic(bridge_data, topic):
    try:
        result = generate_research(topic)
    except Exception as e:
        _log("Gemini failed:", e)
        return
    _log("research generated for Cosmitude message")
    if not send_result_card(result, bridge_data):
        _log("could not deliver the result card to the forum")
    _post_unsolicited(result)


def handle_bridge_message(bridge_data):
    flow = bridge_data.get("FlowID")
    if flow != RESEARCH_FLOW_ID:
        return
    sender = bridge_data.get("MobileNumber")
    if sender and sender == MobileNumber:       # our own message (incl. mirrored topics)
        return
    topic = extract_topic(bridge_data, flow)
    if not topic:
        _log(f"message on {flow!r} but no topic text found; keys={sorted(bridge_data)}. "
             "Set COSMITUDE_INPUT_KEY to the key holding the topic.")
        return
    if _is_own_topic(topic):
        return
    _log("research request from forum:", topic)
    threading.Thread(target=_process_forum_topic, args=(bridge_data, topic),
                     daemon=True).start()


# --------------------------------------------------------------------------
# Mirroring React requests into the forum (BRIDGE_MODE=cosmitude)
# --------------------------------------------------------------------------
def mirror_to_cosmitude(topic, result):
    if not Sessionid:
        _log("not logged in; result not mirrored to the forum")
        return
    try:
        _remember_own_topic(topic)
        _, fields, _ = _flow_info(RESEARCH_FLOW_ID)
        input_id = fields.get("input") or next(iter(fields.values()), None)
        if input_id:
            send_bridge(RESEARCH_FLOW_ID, {input_id: _b64(topic)})
        send_result_card(result)
    except Exception as e:
        _log("mirroring to the forum failed:", e)


# --------------------------------------------------------------------------
# Cosmitude sync loop
# --------------------------------------------------------------------------
def clientSync():
    resp = _post("syncUserData", {
        "SessionID": Sessionid, "MACAddress": "Bridge-Web",
        "ServerID": SERVER_ID, "ServerName": "Bridge",
    })
    body = resp.get("ErrorMessage")
    if not isinstance(body, dict) or "Admin" not in body:
        raise RuntimeError(f"unexpected sync response: ErrorCode={resp.get('ErrorCode')} "
                           f"ErrorMessage={str(body)[:120]!r}")
    admin = body["Admin"]
    try:
        requests.post(f"{RESEARCHDESK_API}/bridge/heartbeat",
                      json={"source": "cosmitude", "items": len(admin)}, timeout=5)
    except requests.RequestException:
        pass

    for data in admin:
        if "Bridge" in data:
            key = hashlib.sha256(json.dumps(data["Bridge"], sort_keys=True).encode()).hexdigest()
            if key in _seen:
                continue
            if len(_seen) > 5000:
                _seen.clear()
            _seen.add(key)
            handle_bridge_message(data["Bridge"])
        elif "UpdatedTemplateJson" in data:
            tpl = data["UpdatedTemplateJson"]
            if tpl.get("ForumID") == forumID:
                _save_template(tpl)


def cosmitude_loop():
    logged_in = False
    while True:
        try:
            if not logged_in:
                login()
                logged_in = True
            clientSync()
        except Exception as e:
            _log("error:", e, "- will re-login")
            logged_in = False
        time.sleep(SYNC_SECONDS)


def go():
    main()


def main():
    make_console_safe()
    if USERNAME and PASSWORD:
        threading.Thread(target=cosmitude_loop, daemon=True, name="cosmitude").start()
    else:
        _log("COSMITUDE_USERNAME/PASSWORD not set: running without the Cosmitude forum")
    print(f"[bridge] BRIDGE_MODE={BRIDGE_MODE}: results "
          f"{'are mirrored into' if BRIDGE_MODE == 'cosmitude' else 'are NOT mirrored into'}"
          " the Cosmitude forum for React requests")
    serve_react_requests(
        on_result=mirror_to_cosmitude if BRIDGE_MODE == "cosmitude" else None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
