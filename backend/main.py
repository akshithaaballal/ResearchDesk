import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

from bridge_client import bridge  # noqa: E402  (after load_dotenv on purpose)
from research_pipeline import normalize_result  # noqa: E402

app = FastAPI(title="ResearchDesk API")

# Vite may be opened as localhost OR 127.0.0.1 (and its port can change), so
# allow any local origin instead of one hard-coded URL.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Bridge-Transport"],
)

import threading
import bridge_agent

@app.on_event("startup")
def startup_event():
    threading.Thread(target=bridge_agent.go, daemon=True).start()

# How long /research waits for the agent (Gemini calls + retries can take a while).
RESULT_TIMEOUT_SECONDS = float(os.getenv("RESULT_TIMEOUT_SECONDS", "180"))
# An agent that hasn't polled for this long is considered not running.
AGENT_STALE_SECONDS = float(os.getenv("AGENT_STALE_SECONDS", "20"))


class ResearchRequest(BaseModel):
    input: str


class BridgeResult(BaseModel):
    request_id: str | None = None
    result: dict


class Heartbeat(BaseModel):
    source: str = "cosmitude"
    items: int | None = None


@app.get("/")
def root():
    return {"message": "ResearchDesk API is running"}


@app.post("/research")
def research(request: ResearchRequest, response: Response):
    # Response body keeps the original flat shape (title/overview/...),
    # so the React app does not need to change its rendering.
    topic = request.input.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Please enter a research topic.")

    if not bridge.agent_alive(AGENT_STALE_SECONDS):
        raise HTTPException(
            status_code=503,
            detail="The research agent is not running. Start it with "
                   "'python bridge_agent.py' in the backend folder, then try again.",
        )

    request_id = bridge.submit(topic)
    response.headers["X-Bridge-Transport"] = bridge.transport

    result = bridge.wait_for_result(request_id, RESULT_TIMEOUT_SECONDS)
    if result is None:
        bridge.discard(request_id)
        raise HTTPException(
            status_code=504,
            detail="The research agent did not return a result in time. "
                   "Check the bridge_agent.py console for errors.",
        )

    if result.get("status") == "error":
        raise HTTPException(
            status_code=502,
            detail=result.get("message") or "The research agent failed to generate a result.",
        )

    try:
        return normalize_result(result, topic)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Research result was empty: {e}")


import httpx
from fastapi import File, UploadFile, Form

ASK_DOCS_URL = "https://mcp.cosmitude.com"

class AskDocsRequest(BaseModel):
    token: str
    question: str
    doc_id: str | None = None

@app.post("/askdocs/upload")
async def askdocs_upload(file: UploadFile = File(...), token: str = Form(...)):
    async with httpx.AsyncClient() as client:
        # Pass file directly to httpx
        files = {'file': (file.filename, file.file.read(), file.content_type)}
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(f"{ASK_DOCS_URL}/catalogue/upload", files=files, headers=headers, timeout=120.0)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()

@app.post("/askdocs/ask")
async def askdocs_ask(req: AskDocsRequest):
    url = f"{ASK_DOCS_URL}/catalogue/ask"
    if req.doc_id:
        url += f"/{req.doc_id}"
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url, 
            json={"question": req.question}, 
            headers={"Authorization": f"Bearer {req.token}"},
            timeout=120.0
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()


# ---- endpoints used by agents (localhost) ---------------------------------
@app.get("/bridge/request")
def get_bridge_request():
    """Returns the oldest unclaimed request once, or null."""
    return bridge.claim_next()


@app.post("/bridge/result")
def receive_bridge_result(body: BridgeResult):
    matched = bridge.complete(body.request_id, body.result)
    return {"status": "received", "matched_request": matched}


@app.post("/bridge/heartbeat")
def bridge_heartbeat(body: Heartbeat):
    bridge.cosmitude_heartbeat(body.items)
    return {"status": "ok"}


@app.get("/bridge/status")
def bridge_status():
    """Wiring status: whether an agent is polling, whether the Cosmitude loop is
    alive, and whether results are mirrored through Cosmitude."""
    return bridge.status()
