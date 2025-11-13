import os, time, json, requests
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, PlainTextResponse

app = FastAPI()

# ==== ENV ====
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ==== CONSTS ====
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"

# In-memory token store (демо)
TOKENS: dict[int, dict] = {}


@app.get("/")
def root():
    return {"status": "ok", "message": "Garmin–Strava–ChatGPT bot is running!"}


# health for HEAD (чтобы не было 405)
@app.head("/")
def root_head():
    return PlainTextResponse("", status_code=200)


# === VERIFY (GET) — Strava challenge ===
@app.get("/strava/webhook")
def verify(request: Request):
    # Strava присылает параметр с точкой в имени
    challenge = (
        request.query_params.get("hub.challenge")
        or request.query_params.get("hub_challenge")
        or request.query_params.get("challenge")
    )
    return JSONResponse({"hub.challenge": challenge or ""}, status_code=200)


# === WEBHOOK (POST) — события от Strava ===
@app.post("/strava/webhook")
async def webhook(req: Request, background_tasks: BackgroundTasks):
    payload = await req.json()
    print("WEBHOOK PAYLOAD:", payload)
    if payload.get("object_type") == "activity" and payload.get("aspect_type") in ("create", "update"):
        owner_id = payload.get("owner_id")
        activity_id = payload.get("object_id")
        background_tasks.add_task(process_activity, owner_id, activity_id)
    return {"ok": True}


# === OAUTH CALLBACK ===
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


def get_access_token(athlete_id: int) -> str:
    t = TOKENS[athlete_id]
    if time.time() > t["expires_at"] - 60:
        rr = requests.post(STRAVA_TOKEN_URL, data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": t["refresh"]
        }).json()
        t["access"] = rr["access_token"]
        t["refresh"] = rr.get("refresh_token", t["refresh"])
        t["expires_at"] = rr["expires_at"]
    return t["access"]


def summarize_week(acts: list[dict]) -> dict:
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    secs = elev = dist = 0.0
    cnt = 0
    for x in acts:
        start = datetime.fromisoformat(x["start_date"].replace("Z", "+00:00"))
        if start > now - timedelta(days=7):
            secs += x.get("moving_time", 0)
            elev += x.get("total_elevation_gain", 0)
            dist += x.get("distance", 0.0)
            cnt += 1
    return {"workouts": cnt, "duration_s": int(secs), "elev_m": int(elev), "dist_m": int(dist)}


def build_coach_prompt(activity: dict, week_summary: dict) -> str:
    goal = os.getenv("COACH_GOAL") or "цель не указана"
    safe = {k: activity.get(k) for k in [
        "name", "type", "distance", "moving_time", "average_heartrate",
        "average_speed", "total_elevation_gain", "suffer_score", "start_date_local"
    ]}
    return f"""
Ты — персональный тренер по выносливости.

ЦЕЛЬ: {goal}

ДАНО:
- Текущая тренировка: {json.dumps(safe, ensure_ascii=False)}
- Сводка 7 дней: {week_summary}

ОТВЕТ В ДВУХ БЛОКАХ:
A) Разбор тренировки — 3–5 пунктов (интенсивность, пульс/темп, набор, техника/каденс).
B) Рекомендация на завтра и корректировка недели — конкретно (минуты/зоны/RPE), с учётом цели.
""".strip()


def ask_openai(prompt: str) -> str:
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": "gpt-5.1-mini", "input": prompt}
    )
    try:
        r.raise_for_status()
        return r.json().get("output_text", "").strip()
    except Exception as e:
        print("OPENAI ERROR:", e, "RAW:", r.text)
        return "Не удалось получить совет (временная ошибка API)."


def send_tg(text: str, chat_id: str | None = None):
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text})
    except Exception as e:
        print("TELEGRAM ERROR:", e)


def process_activity(athlete_id: int, activity_id: int):
    # Дебаг, чтобы видеть поток
    print(f"PROCESS START owner={athlete_id} activity={activity_id}")
    try:
        token = get_access_token(athlete_id)
    except KeyError:
        print(f"SKIP: нет токена для owner={athlete_id}. Пройди OAuth ещё раз.")
        return
    except Exception as e:
        print("ERROR get_access_token:", e)
        return

    headers = {"Authorization": f"Bearer {token}"}
    try:
        a = requests.get(f"{STRAVA_API}/activities/{activity_id}", headers=headers).json()
        acts = requests.get(f"{STRAVA_API}/athlete/activities", headers=headers, params={"per_page": 20}).json()
        week = summarize_week(acts)
        prompt = build_coach_prompt(a, week)
        advice = ask_openai(prompt)
        print("==== COACH ADVICE ====")
        print(advice)
        print("======================")
        # отправка в TG (если токен/чат заданы)
        name = a.get("name"); atype = a.get("type")
        msg = f"Новая тренировка: {name} — {atype}\n\nСовет:\n{advice}"
        send_tg(msg)
    except Exception as e:
        print("PROCESS ERROR:", e)

