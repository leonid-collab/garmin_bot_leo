import os, time, json, requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse, JSONResponse


app = FastAPI()

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"
TOKENS = {}

@app.get("/")
def root():
    return {"status": "ok", "message": "Garmin–Strava–ChatGPT bot is running!"}

@app.get("/strava/webhook")
def verify(request: Request):
    # корректно достаём challenge-параметр, Strava использует точку в имени
    challenge = (
        request.query_params.get("hub.challenge")
        or request.query_params.get("hub_challenge")
        or request.query_params.get("challenge")
    )
    return JSONResponse({"hub.challenge": challenge or ""}, status_code=200)

@app.post("/strava/webhook")
async def webhook(req: Request, background_tasks: BackgroundTasks):
    payload = await req.json()
    if payload.get("object_type") == "activity" and payload.get("aspect_type") in ("create","update"):
        owner_id = payload.get("owner_id")
        activity_id = payload.get("object_id")
        background_tasks.add_task(process_activity, owner_id, activity_id)
    return {"ok": True}

# (необязательно) чтобы не было 405 в логах от health-проверок
@app.head("/")
def root_head():
    return PlainTextResponse("", status_code=200)
@app.get("/strava/oauth/callback")
def oauth_callback(code: str):
    r = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code"
    }).json()
    athlete_id = r["athlete"]["id"]
    TOKENS[athlete_id] = {
        "access": r["access_token"],
        "refresh": r["refresh_token"],
        "expires_at": r["expires_at"]
    }
    return PlainTextResponse(f"✅ Strava подключена! Athlete ID: {athlete_id}")

def get_access_token(athlete_id: int):
    t = TOKENS[athlete_id]
    if time.time() > t["expires_at"] - 60:
        rr = requests.post(STRAVA_TOKEN_URL, data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": t["refresh"]
        }).json()
        t["access"] = rr["access_token"]
        t["refresh"] = rr["refresh_token"]
        t["expires_at"] = rr["expires_at"]
    return t["access"]

def process_activity(athlete_id: int, activity_id: int):
    token = get_access_token(athlete_id)
    headers = {"Authorization": f"Bearer {token}"}
    a = requests.get(f"{STRAVA_API}/activities/{activity_id}", headers=headers).json()
    acts = requests.get(f"{STRAVA_API}/athlete/activities", headers=headers, params={"per_page": 10}).json()
    summary = summarize_week(acts)
    advice = ask_openai(a, summary)
    print("💬 AI Coach:\n", advice)

def summarize_week(acts):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    dur = elev = 0
    for x in acts:
        start = datetime.fromisoformat(x["start_date"].replace("Z","+00:00"))
        if start > now - timedelta(days=7):
            dur += x.get("moving_time",0)
            elev += x.get("total_elevation_gain",0)
    return {"duration": dur, "elev": elev}

def ask_openai(activity, week):
    text = f"""
Ты спортивный тренер. 
Дай краткий анализ активности и рекомендацию на завтра.
Активность: {json.dumps(activity, ensure_ascii=False)[:1000]}
Неделя: {week}
"""
    r = requests.post("https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type":"application/json"},
        json={"model": "gpt-5.1-mini", "input": text})
    return r.json().get("output_text", "")
