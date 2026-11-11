"""
Автозаход на пару в LMS TSUE (lms.tsue.uz).

Что делает:
  1) входит через HEMIS (talaba.tsue.uz) и получает JWT;
  2) читает расписание на сегодня через API (без хардкода пар в файле);
  3) за минуту до начала открывает пару и заходит в BigBlueButton;
  4) в диалоге аудио нажимает «Только слушать» (микрофон не трогает);
  5) держит сессию до конца пары и корректно выходит.

Дополнительно: caffeinate не даёт Mac уснуть, а --wake ставит pmset-пробуждения
перед парами (нужен пароль администратора).

Запуск:
    python3 lms_auto.py                  # планировщик: ждёт пары сегодня
    python3 lms_auto.py --list           # показать расписание на сегодня
    python3 lms_auto.py --now            # зайти в пару, которая идёт сейчас
    python3 lms_auto.py --now --stay 5   # то же, но выйти через 5 минут (тест)
"""
import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

HEMIS_LOGIN = os.environ.get("HEMIS_LOGIN", "")
HEMIS_PASSWORD = os.environ.get("HEMIS_PASSWORD", "")
HEADLESS = os.environ.get("HEADLESS", "0") == "1"
# для сервера/контейнера иногда нужен --no-sandbox: CHROMIUM_ARGS="--no-sandbox"
CHROMIUM_ARGS = os.environ.get("CHROMIUM_ARGS", "").split()

LMS_URL = "https://lms.tsue.uz"
API_URL = "https://api-lms.tsue.uz"
TOKEN_KEY = "LMS_STUDENT_ls_user_access"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

SHOTS = BASE / "screenshots"
SHOTS.mkdir(exist_ok=True)

EARLY_START_MIN = 1   # за сколько минут до начала заходить
RETRY_SEC = 30        # как часто проверять расписание


def local(value: str) -> dt.datetime:
    """Время из API (с часовым поясом) -> локальное наивное время."""
    return dt.datetime.fromisoformat(value).astimezone().replace(tzinfo=None)


def short_name(name: str) -> str:
    """«Mikroiqtisodiyot, Mikroiqtisodiyot» -> «Mikroiqtisodiyot»."""
    parts = []
    for part in name.split(", "):
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def shot(page, name: str) -> None:
    try:
        page.screenshot(path=str(SHOTS / f"{dt.datetime.now():%H%M%S}_{name}.png"))
    except Exception:
        pass


# ------------------------------------------------------------------ API

def api(token: str, path: str, params: Optional[dict] = None, timeout: int = 25):
    url = API_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Origin": LMS_URL,
        "Referer": LMS_URL + "/",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def lessons_today(token: str, day: Optional[dt.date] = None) -> list:
    d = (day or dt.date.today()).isoformat()
    data = api(token, "/api/dars/talaba/darslarim/", {"dan": d, "gacha": d})
    return data.get("results", [])


def join_link(token: str, lesson_id: str) -> str:
    """Аналог кнопки «Darsga kirish»: возвращает ссылку на комнату BBB."""
    data = api(token, f"/api/dars/talaba/darsxona/{lesson_id}/kirish/", {"redirect": "false"})
    for key in ("url", "link", "redirect_url", "join_url", "darsxona_url", "bbb_url"):
        if isinstance(data, dict) and data.get(key):
            return data[key]
    for v in (data.values() if isinstance(data, dict) else []):
        if isinstance(v, str) and v.startswith("http"):
            return v
    raise RuntimeError(f"В ответе API нет ссылки: {str(data)[:300]}")


# -------------------------------------------------------------- браузер

def login(page) -> str:
    log("LMS: вход через HEMIS...")
    page.goto(LMS_URL + "/login", wait_until="domcontentloaded")
    page.get_by_text("HEMIS orqali kirish").first.click()
    page.wait_for_url("**/talaba.tsue.uz/**", timeout=40000)

    page.locator("input[name='FormStudentLogin[login]']").fill(HEMIS_LOGIN)
    page.locator("input[name='FormStudentLogin[password]']").fill(HEMIS_PASSWORD)
    page.get_by_role("button", name="Kirish", exact=True).click()

    # страница согласия HEMIS (OAuth): «Ruxsat berish» — разрешить доступ к LMS
    for name in ("Ruxsat berish", "Разрешить", "Allow"):
        try:
            btn = page.get_by_role("button", name=name)
            btn.wait_for(state="visible", timeout=6000)
            btn.click(timeout=5000)
            log(f"HEMIS: подтвердил доступ («{name}»).")
            break
        except Exception:
            pass

    page.wait_for_url(f"{LMS_URL}/**", timeout=40000)

    token = None
    for _ in range(40):
        try:
            token = page.evaluate(f"() => localStorage.getItem('{TOKEN_KEY}')")
        except Exception:
            token = None  # страница ещё перерисовывается после редиректа
        if token:
            break
        page.wait_for_timeout(500)
    if not token:
        raise RuntimeError("Не получил JWT после входа")
    log("LMS: авторизация успешна.")
    return token


LISTEN_ONLY_SELECTORS = (
    '[data-test="listenOnlyBtn"]',
    'button:has-text("Только слушать")',
    'button:has-text("Listen only")',
    'button:has-text("Faqat tinglash")',
    'button[aria-label*="Только слушать"]',
    'button[aria-label*="Listen only" i]',
)


def join_audio_listen_only(page, wait_sec: int = 30) -> bool:
    """Жмёт «Только слушать» в диалоге аудио (RU/EN/UZ). Микрофон не нажимаем никогда."""
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        for sel in LISTEN_ONLY_SELECTORS:
            try:
                loc = page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=2500)
                    log('BBB: аудио — нажал «Только слушать».')
                    page.wait_for_timeout(1500)
                    return True
            except Exception:
                pass
        page.wait_for_timeout(1000)
    log("BBB: диалог аудио не появился (возможно, уже подключён).")
    return False


def join_lesson(page, url: str, title: str) -> bool:
    log(f"Подключаюсь к паре: {title}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    for _ in range(60):
        try:
            if "BigBlueButton" in page.title():
                break
        except Exception:
            pass
        page.wait_for_timeout(1000)
    log(f"BBB: {page.title()}")

    # диалог звука BBB: всегда «Только слушать», микрофон не трогаем
    join_audio_listen_only(page)

    page.wait_for_timeout(4000)
    shot(page, "joined")

    body = ""
    try:
        body = page.inner_text("body")
    except Exception:
        pass
    joined = any(x in body for x in ("(You)", "USERS", "Foydalanuvchilar"))
    log("Уже в паре." if joined else "BBB открыт, но список участников не виден (проверь скриншот).")
    return joined


def in_meeting(page) -> bool:
    try:
        if page.is_closed() or "BigBlueButton" not in page.title():
            return False
        body = page.inner_text("body")
        return any(x in body for x in ("(You)", "USERS", "Foydalanuvchilar"))
    except Exception:
        return False


def attend(page, token: str, lesson: dict, end: dt.datetime, max_fails: int = 20) -> None:
    """Держит пару до конца; при обрыве связи или кике переподключается сам."""
    name = short_name(lesson["fan_name"])
    log(f"Держу пару до {end:%H:%M} ({name}).")
    connected = False
    fails = 0
    while dt.datetime.now() < end:
        if not connected:
            if fails >= max_fails:
                log(f"{max_fails} неудачных попыток — бросаю эту пару.")
                return
            try:
                join_lesson(page, join_link(token, lesson["id"]), name)
                connected = True
                fails = 0
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    log("Токен истёк — вхожу заново.")
                    token = login(page)
                fails += 1
                log(f"Не зашёл ({fails}/{max_fails}): {e}")
                time.sleep(20)
                continue
        time.sleep(30)
        if connected and not in_meeting(page):
            connected = False
            log("Связь с парой потеряна — переподключаюсь.")
            shot(page, "reconnect")
    log("Пара закончилась.")


def leave(page) -> None:
    for sel in ("button[aria-label*='Leave' i]",
                "button:has-text('Leave meeting')",
                "button:has-text('Chiqish')"):
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=3000)
                page.wait_for_timeout(1500)
                break
        except Exception:
            pass
    try:
        page.goto("about:blank")
    except Exception:
        pass


# --------------------------------------------------------- сон и пробуждение

def keep_awake():
    """caffeinate не даёт Mac уснуть, пока работает планировщик (без sudo)."""
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.Popen(["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("caffeinate: Mac не будет засыпать, пока работает планировщик.")
        return proc
    except Exception as e:
        log(f"caffeinate не запустился: {e}")
        return None


def upcoming(token: str, days: int = 7) -> list:
    """Пары на ближайшие N дней (для планирования пробуждений)."""
    today = dt.date.today()
    result = []
    for i in range(days):
        day = today + dt.timedelta(days=i)
        result.extend(lessons_today(token, day))
    return [x for x in result if local(x["tugash"]) > dt.datetime.now()]


def wake_plan(token: str, days: int = 7) -> list:
    """Команды pmset: разбудить Mac за 5 минут до каждой пары."""
    cmds = []
    for lesson in upcoming(token, days):
        start = local(lesson["boshlanish"]) - dt.timedelta(minutes=5)
        if start <= dt.datetime.now():
            continue
        stamp = start.strftime("%m/%d/%y %H:%M:%S")
        cmds.append((short_name(lesson["fan_name"]), start,
                     f'sudo pmset schedule wake "{stamp}"'))
    return cmds


def schedule_wake(token: str, days: int = 7) -> None:
    """Ставит пробуждения: сначала без пароля, иначе через системный запрос пароля."""
    plan = wake_plan(token, days)
    if not plan:
        log("Будить не под что — пар в ближайшие дни нет.")
        return

    try:
        for _, _, cmd in plan:
            subprocess.run(["sudo", "-n"] + shlex.split(cmd)[1:],
                           check=True, capture_output=True, timeout=15)
        for name, start, _ in plan:
            log(f"Пробуждение: {start:%d.%m %H:%M} ({name})")
        return
    except Exception:
        pass

    # один системный запрос пароля на все будильники
    script = " && ".join(c.replace('"', '\\"') for _, _, c in plan)
    try:
        subprocess.run(["osascript", "-e",
                        f'do shell script "{script}" with administrator privileges'],
                       check=True, capture_output=True, timeout=300)
        for name, start, _ in plan:
            log(f"Пробуждение: {start:%d.%m %H:%M} ({name})")
    except Exception as e:
        log(f"Не удалось поставить пробуждения ({e}). Выполни вручную:")
        for _, _, cmd in plan:
            print(cmd)


# ------------------------------------------------------------------ CLI

def show(lessons: list) -> None:
    if not lessons:
        print("На сегодня пар нет.")
        return
    for item in lessons:
        b = local(item["boshlanish"])
        e = local(item["tugash"])
        access = "вход открыт" if item["kirish_ochiqmi"] else "вход закрыт"
        print(f"{b:%H:%M}-{e:%H:%M}  {short_name(item['fan_name']):<45} "
              f"{item['holat_label']:<16} {access}")


def run_lesson(page, token: str, lesson: dict, stay_min: Optional[int] = None) -> None:
    start = local(lesson["boshlanish"])
    end = local(lesson["tugash"])
    if stay_min:
        end = min(end, dt.datetime.now() + dt.timedelta(minutes=stay_min))
    log(f"Пара: {short_name(lesson['fan_name'])} ({start:%H:%M}-{end:%H:%M})")
    attend(page, token, lesson, end)
    leave(page)


def scheduler(not_before: Optional[dt.time] = None) -> None:
    """Работает бесконечно: при любом падении перезапускается сам."""
    log("Планировщик запущен (Ctrl+C — остановить).")
    keep_awake()
    while True:
        try:
            run_scheduler(not_before)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"Планировщик упал: {e}. Перезапуск через 60 сек.")
            time.sleep(60)


def run_scheduler(not_before: Optional[dt.time] = None) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=CHROMIUM_ARGS)
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        token = login(page)
        done = set()
        try:
            while True:
                try:
                    lessons = lessons_today(token)
                except urllib.error.HTTPError as e:
                    if e.code not in (401, 403):
                        raise
                    log("Токен истёк — вхожу заново.")
                    token = login(page)
                    lessons = lessons_today(token)

                now = dt.datetime.now()
                for lesson in lessons:
                    lid = lesson["id"]
                    if lid in done or lesson["holat"] == "tugagan" or not lesson["kirish_ochiqmi"]:
                        continue
                    start = local(lesson["boshlanish"])
                    end = local(lesson["tugash"])
                    if not_before and start.time() < not_before:
                        done.add(lid)
                        continue
                    if now > end:
                        done.add(lid)
                        continue
                    if now >= start - dt.timedelta(minutes=EARLY_START_MIN):
                        done.add(lid)
                        try:
                            run_lesson(page, token, lesson)
                        except Exception as e:
                            log(f"Ошибка при входе в пару: {e}")
                            shot(page, "error")
                time.sleep(RETRY_SEC)
        finally:
            browser.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="зайти в пару, которая идёт сейчас")
    ap.add_argument("--list", action="store_true", help="показать расписание на сегодня")
    ap.add_argument("--stay", type=int, help="остаться N минут (для теста)")
    ap.add_argument("--date", help="дата для --list (YYYY-MM-DD)")
    ap.add_argument("--from-time", metavar="HH:MM",
                    help="не входить в пары, которые начались раньше этого времени")
    ap.add_argument("--wake-plan", action="store_true",
                    help="показать команды pmset для пробуждения Mac перед парами")
    ap.add_argument("--wake", action="store_true",
                    help="поставить пробуждения через pmset (нужен sudo)")
    args = ap.parse_args()

    if not HEMIS_LOGIN or not HEMIS_PASSWORD:
        sys.exit("Заполни HEMIS_LOGIN и HEMIS_PASSWORD в .env")

    if args.list or args.now or args.wake_plan or args.wake:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS, args=CHROMIUM_ARGS)
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            try:
                token = login(page)
                if args.list:
                    show(lessons_today(token, dt.date.fromisoformat(args.date) if args.date else None))
                    return
                if args.wake_plan:
                    for name, start, cmd in wake_plan(token):
                        print(f"{start:%d.%m %H:%M}  {name:<32} {cmd}")
                    return
                if args.wake:
                    schedule_wake(token)
                    return
                now = dt.datetime.now()
                current = [x for x in lessons_today(token)
                           if x["kirish_ochiqmi"] and x["holat"] != "tugagan"
                           and local(x["boshlanish"]) <= now
                           <= local(x["tugash"])]
                if not current:
                    show(lessons_today(token))
                    sys.exit("Сейчас пар с открытым входом нет.")
                run_lesson(page, token, current[0], args.stay)
            finally:
                browser.close()
        return

    scheduler(dt.time.fromisoformat(args.from_time) if args.from_time else None)


if __name__ == "__main__":
    main()
