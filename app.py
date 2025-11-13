import os
import time
import json
import requests
from typing import Dict, List, Any, Optional

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse, JSONResponse

# ================== НАСТРОЙКИ И СТАРТ ==================

app = FastAPI()

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"

# In-memory хранилище токенов: athlete_id -> {access, refresh, expires_at}
TOKENS: Dict[int, Dict[str, Any]] = {}

print("[ENV] STRAVA_CLIENT_ID:", STRAVA_CLIENT_ID)
print("[ENV] STRAVA_CLIENT_SECRET set:", bool(STRAVA_CLIENT_SECRET))
print("[ENV] OPENAI_API_KEY set:", bool(OPENAI_API_KEY))


# ================== БАЗОВЫЕ ЭНДПОИНТЫ ==================


@app.get("/")
def root():
    return {"status": "ok", "message": "Garmin–Strava–ChatGPT bot is running!"}


@app.head("/")
def root_head():
    # чтобы health-проверки HEAD не сыпали 405 в логах
    return PlainTextResponse("", status_code=200)


# ================== STRAVA WEBHOOK VERIFY (GET) ==================


@app.get("/strava/webhook")
def verify(request: Request):
    """
    Strava при создании подписки делает GET с параметром hub.challenge.
    Мы обязаны вернуть {"hub.challenge": "<значение>"}.
    """
    challenge = (
        request.query_params.get("hub.challenge")
        or request.query_params.get("hub_challenge")
        or request.query_params.get("challenge")
    )
    print("[VERIFY] hub.challenge =", challenge)
    return JSONResponse({"hub.challenge": challenge or ""}, status_code=200)


# ================== STRAVA WEBHOOK EVENTS (POST) ==================


@app.post("/strava/webhook")
async def webhook(req: Request, background_tasks: BackgroundTasks):
    payload = await req.json()
    print("=== WEBHOOK IN ===")
    print(payload)
    print("==================")

    object_type = payload.get("object_type")
    aspect_type = payload.get("aspect_type")
    owner_id = payload.get("owner_id")
    activity_id = payload.get("object_id")

    print(f"[WEBHOOK] object_type={object_type} aspect_type={aspect_type} "
          f"owner={owner_id} activity={activity_id}")

    if object_type == "activity" and aspect_type in ("create", "update"):
        print("[WEBHOOK] queue process_activity")
        background_tasks.add_task(process_activity, owner_id, activity_id)
    else:
        print("[WEBHOOK] not activity/create/update — skip")

    return {"ok": True}


# ================== STRAVA OAUTH CALLBACK ==================


@app.get("/strava/oauth/callback")
def oauth_callback(code: str):
    """
    Сюда приходит Strava после авторизации.
    Обмениваем code на access/refresh токены и кладём в TOKENS.
    """
    print("[OAUTH] callback with code:", code)
    r = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    print("[OAUTH] status:", r.status_code)
    print("[OAUTH] raw:", r.text[:1000])

    r.raise_for_status()
    data = r.json()
    athlete_id = data["athlete"]["id"]
    TOKENS[athlete_id] = {
        "access": data["access_token"],
        "refresh": data["refresh_token"],
        "expires_at": data["expires_at"],
    }
    print(f"[OAUTH] athlete {athlete_id} tokens stored")

    return PlainTextResponse(f"✅ Strava подключена! Athlete ID: {athlete_id}")


# ================== РАБОТА С ТОКЕНАМИ STRAVA ==================


def get_access_token(athlete_id: int) -> str:
    """
    Получаем access_token для атлета. При необходимости — обновляем по refresh_token.
    """
    if athlete_id not in TOKENS:
        raise KeyError(f"no tokens for athlete_id={athlete_id}")

    t = TOKENS[athlete_id]
    now = time.time()
    if now > t["expires_at"] - 60:
        print(f"[TOKENS] refreshing token for athlete={athlete_id}")
        rr = requests.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": STRAVA_CLIENT_ID,
                "client_secret": STRAVA_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": t["refresh"],
            },
        )
        print("[TOKENS] refresh status:", rr.status_code, "raw:", rr.text[:500])
        rr.raise_for_status()
        data = rr.json()
        t["access"] = data["access_token"]
        t["refresh"] = data.get("refresh_token", t["refresh"])
        t["expires_at"] = data["expires_at"]

    return t["access"]


# ================== УТИЛИТЫ ДЛЯ АНАЛИЗА НЕДЕЛИ ==================


def summarize_week(acts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Сводка за 7 дней по последним активностям из Strava.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    dur = 0.0
    dist = 0.0
    elev = 0.0
    cnt = 0

    for x in acts:
        try:
            start = datetime.fromisoformat(
                x["start_date"].replace("Z", "+00:00")
            )
        except Exception:
            continue

        if start > now - timedelta(days=7):
            dur += x.get("moving_time", 0) or 0
            dist += x.get("distance", 0.0) or 0.0
            elev += x.get("total_elevation_gain", 0.0) or 0.0
            cnt += 1

    return {
        "workouts": cnt,
        "duration_s": int(dur),
        "dist_m": int(dist),
        "elev_m": int(elev),
    }


def is_moving_activity(activity: Dict[str, Any]) -> bool:
    """
    Простая проверка, что активность — не пустая.
    Можно подстроить пороги под себя.
    """
    atype = activity.get("type", "")
    dist = activity.get("distance", 0) or 0
    moving = activity.get("moving_time", 0) or 0

    # полностью пустая
    if dist <= 0 and moving <= 0:
        return False

    # совсем короткая (тычок)
    if moving < 60 and dist < 200:
        return False

    # здесь можно добавить фильтры по типам (VirtualRide, EBikeRide и т.п.)
    # if atype in ("VirtualRide", "EBikeRide"):
    #     return False

    return True


# ================== GPT: ПРОМПТ И ВЫЗОВ OPENAI ==================


def build_coach_prompt(activity: Dict[str, Any], week_summary: Dict[str, Any]) -> str:
    goal = os.getenv("COACH_GOAL") or "цель не указана"

    safe = {k: activity.get(k) for k in [
        "name",
        "type",
        "sport_type",
        "distance",
        "moving_time",
        "elapsed_time",
        "average_speed",
        "average_heartrate",
        "max_heartrate",
        "total_elevation_gain",
        "suffer_score",
        "start_date_local",
    ]}

    return f"""
Ты — персональный тренер по выносливости (бег, трейл, вело).

ЦЕЛЬ АТЛЕТА: {goal}

ДАНО:
- Текущая тренировка (основные поля из Strava): {json.dumps(safe, ensure_ascii=False)}
- Сводка за 7 дней: {week_summary}

ОТВЕТ СТРОГО В ДВУХ БЛОКАХ:

A) Краткий разбор тренировки (3–6 пунктов):
   - нагрузка (легко/средне/тяжело),
   - пульс/темп относительно целей,
   - набор высоты, техника, усталость,
   - были ли признаки перегруза.

B) Конкретная рекомендация:
   - что делать ЗАВТРА (тип, длительность, интенсивность/зона/по RPE),
   - как скорректировать оставшиеся дни недели под цель,
   - если есть риск перегруза — явно скажи, как уменьшить объём/интенсивность.
""".strip()


def ask_openai(prompt: str) -> str:
    if not OPENAI_API_KEY:
        print("[GPT] ERROR: OPENAI_API_KEY не задан!")
        return "Не удалось получить совет: не настроен ключ OpenAI."

    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-5.1-mini",
                "input": prompt,
            },
            timeout=30,
        )
        print("[GPT] HTTP status:", r.status_code)
        print("[GPT] RAW:", r.text[:1000])

        r.raise_for_status()
        data = r.json()
        txt = data.get("output_text", "").strip()
        if not txt:
            print("[GPT] WARNING: output_text пустой")
            return "Модель вернула пустой ответ."
        return txt
    except Exception as e:
        print("[GPT] ERROR:", repr(e))
        return "Не удалось получить совет (ошибка при обращении к OpenAI)."


# ================== ОТПРАВКА В TELEGRAM (ОПЦИОНАЛЬНО) ==================


def send_tg(text: str, chat_id: Optional[str] = None) -> None:
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TG_CHAT_ID")

    if not token or not chat_id:
        print("[TG] TG_BOT_TOKEN или TG_CHAT_ID не заданы — не отправляем.")
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        print("[TG] status:", resp.status_code, "raw:", resp.text[:500])
    except Exception as e:
        print("[TG] ERROR:", repr(e))


# ================== ГЛАВНАЯ ЛОГИКА ОБРАБОТКИ АКТИВНОСТИ ==================


def process_activity(athlete_id: int, activity_id: int):
    print("=== PROCESS START ===")
    print(f"owner={athlete_id} activity={activity_id}")

    # 1) берём токен
    try:
        token = get_access_token(athlete_id)
        print("[PROCESS] access token OK")
    except KeyError:
        print(f"[PROCESS] SKIP: нет токена для owner={athlete_id}. Надо пройти OAuth.")
        return
    except Exception as e:
        print("[PROCESS] ERROR get_access_token:", repr(e))
        return

    headers = {"Authorization": f"Bearer {token}"}

    # 2) детальная активность
    try:
        r_act = requests.get(
            f"{STRAVA_API}/activities/{activity_id}",
            headers=headers,
            timeout=15,
        )
        print("[STRAVA] /activities status:", r_act.status_code)
        print("[STRAVA] /activities raw:", r_act.text[:500])
        if r_act.status_code != 200:
            print("[STRAVA] ERROR: не смогли получить активность")
            return
        activity = r_act.json()
    except Exception as e:
        print("[PROCESS] ERROR fetching activity:", repr(e))
        return

    # 2a) проверка, что это не мусорная активность
    if not is_moving_activity(activity):
        print(f"[PROCESS] SKIP activity {activity_id}: без движения / слишком короткая")
        return

    # 3) список активностей для сводки
    try:
        r_list = requests.get(
            f"{STRAVA_API}/athlete/activities",
            headers=headers,
            params={"per_page": 50},
            timeout=15,
        )
        print("[STRAVA] /athlete/activities status:", r_list.status_code)
        if r_list.status_code != 200:
            print("[STRAVA] ERROR: не смогли получить список активностей")
            acts: List[Dict[str, Any]] = []
        else:
            acts = r_list.json()
            print(f"[STRAVA] /athlete/activities count={len(acts)}")
    except Exception as e:
        print("[PROCESS] ERROR fetching activities list:", repr(e))
        acts = []

    week_summary = summarize_week(acts)

    # 4) формируем промпт
    try:
        prompt = build_coach_prompt(activity, week_summary)
        print("=== GPT PROMPT PREVIEW ===")
        print(prompt[:800])
        print("=== END PROMPT PREVIEW ===")
    except Exception as e:
        print("[PROCESS] ERROR build_coach_prompt:", repr(e))
        return

    # 5) вызываем GPT
    advice = ask_openai(prompt)
    print("=== COACH ADVICE ===")
    print(advice)
    print("====================")

    # 6) отправляем в Telegram (если настроен)
    name = activity.get("name")
    atype = activity.get("type")
    msg = f"Новая тренировка: {name} — {atype}\n\nСовет:\n{advice}"
    send_tg(msg)

    print("=== PROCESS END ===")


# ================== ПЛАН НА НЕДЕЛЮ ПО URL ==================


@app.get("/plan/weekly")
def weekly_plan():
    """
    Ручной триггер: дергаешь URL → в Телеграм прилетает план на неделю,
    построенный по последним тренировкам + цели (COACH_GOAL).
    """
    if not TOKENS:
        return PlainTextResponse(
            "Нет подключённого атлета (надо пройти OAuth через Strava).",
            status_code=400,
        )

    athlete_id = list(TOKENS.keys())[0]
    print("[PLAN] using athlete_id:", athlete_id)

    try:
        token = get_access_token(athlete_id)
    except Exception as e:
        print("[PLAN] ERROR get_access_token:", repr(e))
        return PlainTextResponse("Ошибка токена Strava", status_code=500)

    headers = {"Authorization": f"Bearer {token}"}
    try:
        r_list = requests.get(
            f"{STRAVA_API}/athlete/activities",
            headers=headers,
            params={"per_page": 50},
            timeout=15,
        )
        print("[PLAN] /athlete/activities status:", r_list.status_code)
        r_list.raise_for_status()
        acts = r_list.json()
    except Exception as e:
        print("[PLAN] ERROR fetching activities:", repr(e))
        return PlainTextResponse("Ошибка при запросе к Strava", status_code=500)

    week_summary = summarize_week(acts)
    goal = os.getenv("COACH_GOAL", "цель не указана")

    prompt = f"""
Ты тренер по выносливости. На основе последних тренировок (сводка ниже)
и цели атлета составь план на следующую неделю (5–7 дней).

ЦЕЛЬ: {goal}

СВОДКА НЕДЕЛИ: {week_summary}

Выведи по дням:
- ДЕНЬ недели,
- тип тренировки,
- длительность,
- интенсивность (зона / RPE),
- если нужен отдых — так и напиши.
""".strip()

    advice = ask_openai(prompt)
    send_tg("📅 План на неделю:\n" + advice)

    return PlainTextResponse("План отправлен в Telegram ✅")
