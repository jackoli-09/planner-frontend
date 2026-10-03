"""
E2E: офлайн → онлайн → второе устройство.

Поднимает статический фронтенд и backend (из соседнего репозитория planner-backend)
с тестовым BOT_TOKEN, подменяет Telegram SDK заглушкой с подписанным initData.

Переменные:
  BACKEND_DIR        путь к planner-backend (по умолчанию ../planner-backend)
  TEST_DATABASE_URL  PostgreSQL для backend
Запуск: pytest -q tests/e2e
"""
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(os.environ.get("BACKEND_DIR", ROOT.parent / "planner-backend"))
BOT_TOKEN = "123456:TEST_TOKEN_FOR_TESTS_ONLY"
FRONT_PORT, API_PORT = 8134, 8765
RAILWAY = "https://planner-backend-production-ad6d.up.railway.app"
OWNER_ID = 1_999_999_999
CHART_JS = Path(os.environ.get("CHART_JS", ROOT.parent / "e2edeps/node_modules/chart.js/dist/chart.umd.js"))


def init_data(user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "E2E"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={values[k]}" for k in sorted(values))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def telegram_stub(user_id: int) -> str:
    raw = init_data(user_id)
    return f"""
    window.Telegram = {{ WebApp: {{
      initData: {json.dumps(raw)},
      initDataUnsafe: {{ user: {{ id: {user_id}, first_name: 'E2E' }} }},
      ready(){{}}, expand(){{}}, onEvent(){{}}, offEvent(){{}}, setHeaderColor(){{}},
      setBackgroundColor(){{}}, HapticFeedback: {{ impactOccurred(){{}}, notificationOccurred(){{}}, selectionChanged(){{}} }},
      colorScheme: 'dark', themeParams: {{}}, platform: 'tdesktop', version: '7.0',
      BackButton: {{ show(){{}}, hide(){{}}, onClick(){{}}, offClick(){{}} }},
      MainButton: {{ show(){{}}, hide(){{}}, setText(){{}}, onClick(){{}}, offClick(){{}} }},
    }} }};
    """


def wait_port(port: int, timeout: float = 30):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"port {port} did not open")


@pytest.fixture(scope="module")
def servers():
    env = os.environ | {
        "BOT_TOKEN": BOT_TOKEN, "APP_ENV": "test", "ALLOW_INSECURE_DEMO": "false",
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/planner"),
        "ALLOWED_ORIGINS": f"http://localhost:{FRONT_PORT}",
        "FRONTEND_URL": f"http://localhost:{FRONT_PORT}",
        "OWNER_USER_ID": str(OWNER_ID),
    }
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(API_PORT)],
                           cwd=BACKEND_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    web = subprocess.Popen([sys.executable, "-m", "http.server", str(FRONT_PORT), "--bind", "127.0.0.1"],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(API_PORT); wait_port(FRONT_PORT)
        for _ in range(50):
            try:
                if urlopen(f"http://127.0.0.1:{API_PORT}/api/health").status == 200:
                    break
            except Exception:
                time.sleep(0.2)
        yield
    finally:
        api.terminate(); web.terminate()


class Device:
    """Отдельный браузерный контекст = отдельное устройство того же пользователя."""

    def __init__(self, browser, user_id: int):
        self.api_down = False
        self.ctx = browser.new_context(viewport={"width": 390, "height": 844}, service_workers="allow")
        self.ctx.route("https://telegram.org/js/telegram-web-app.js",
                       lambda r: r.fulfill(content_type="application/javascript", body=telegram_stub(user_id)))
        self.ctx.route(f"{RAILWAY}/**", self._proxy_api)
        # внешние CDN в CI/песочнице могут быть недоступны — отдаём локальные копии
        self.ctx.route("https://cdnjs.cloudflare.com/**",
                       lambda r: r.fulfill(content_type="application/javascript", path=str(CHART_JS)))
        self.ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(content_type="text/css", body=""))
        self.ctx.route("https://world.openfoodfacts.org/**", lambda r: r.abort())
        self.ctx.add_init_script(f"localStorage.setItem('planner_profile_skipped_{user_id}','1')")
        self.page = self.ctx.new_page()

    def _proxy_api(self, route):
        if self.api_down:
            return route.abort("internetdisconnected")
        url = route.request.url.replace(RAILWAY, f"http://127.0.0.1:{API_PORT}")
        route.fulfill(response=route.fetch(url=url))

    def open(self):
        self.page.goto(f"http://localhost:{FRONT_PORT}/index.html")
        self.page.wait_for_function("typeof openAddTask === 'function' && typeof getOutbox === 'function'")

    def add_task(self, text: str):
        self.page.evaluate("openAddTask()")
        self.page.fill("#m-task-text", text)
        self.page.click("#m-task-submit")

    def task_texts(self):
        return self.page.evaluate("state.tasks.map(t => t.text)")

    def outbox_len(self):
        return self.page.evaluate("getOutbox().length")


@pytest.fixture()
def browser(servers):
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def test_offline_changes_reach_second_device(browser):
    uid = 2_000_000_000 + uuid.uuid4().int % 10**8
    text = "Офлайн-задача " + uuid.uuid4().hex[:6]

    phone = Device(browser, uid)
    phone.open()
    phone.page.wait_for_function("getOutbox().length === 0")

    # 1. Сервер недоступен: задача сохраняется локально и встаёт в очередь
    phone.api_down = True
    phone.add_task(text)
    assert text in phone.task_texts()
    phone.page.wait_for_function("getOutbox().length > 0")

    # 2. Перезапуск без сервера: данные на месте
    phone.page.reload()
    phone.page.wait_for_function("typeof state !== 'undefined' && Array.isArray(state.tasks)")
    assert text in phone.task_texts()
    assert phone.outbox_len() > 0

    # 3. Сеть вернулась: очередь уходит на сервер
    phone.api_down = False
    phone.page.evaluate("syncAcrossDevices()")
    phone.page.wait_for_function("getOutbox().length === 0", timeout=15000)

    # 4. Второе устройство видит задачу
    laptop = Device(browser, uid)
    laptop.open()
    laptop.page.wait_for_function(f"state.tasks.some(t => t.text === {json.dumps(text)})", timeout=15000)


def test_offline_delete_does_not_resurrect(browser):
    uid = 2_100_000_000 + uuid.uuid4().int % 10**8
    text = "Удаляемая " + uuid.uuid4().hex[:6]

    phone = Device(browser, uid)
    phone.open()
    phone.add_task(text)
    phone.page.wait_for_function("getOutbox().length === 0", timeout=15000)
    task_id = phone.page.evaluate(f"state.tasks.find(t => t.text === {json.dumps(text)}).id")

    phone.api_down = True
    phone.page.evaluate(f"deleteTask({json.dumps(task_id)})")
    assert text not in phone.task_texts()

    phone.api_down = False
    phone.page.evaluate("syncAcrossDevices()")
    phone.page.wait_for_function("getOutbox().length === 0", timeout=15000)

    laptop = Device(browser, uid)
    laptop.open()
    laptop.page.wait_for_function("getOutbox().length === 0")
    laptop.page.wait_for_timeout(1500)
    assert text not in laptop.task_texts()


def test_other_user_sees_nothing(browser):
    owner = 2_200_000_000 + uuid.uuid4().int % 10**8
    text = "Личное " + uuid.uuid4().hex[:6]
    a = Device(browser, owner)
    a.open()
    a.add_task(text)
    a.page.wait_for_function("getOutbox().length === 0", timeout=15000)

    b = Device(browser, owner + 1)
    b.open()
    b.page.wait_for_timeout(1500)
    assert text not in b.task_texts()


def test_feedback_and_owner_stats(browser):
    user = Device(browser, 2_300_000_000 + uuid.uuid4().int % 10**8)
    user.open()
    user.page.wait_for_function("getOutbox().length === 0")
    assert user.page.is_hidden("#owner-stats-btn")
    user.page.evaluate("openFeedback()")
    user.page.fill("#feedback-text", "Хочу таймер отдыха")
    user.page.click("#feedback-submit")
    expect(user.page.locator("#feedback-status")).to_contain_text("Спасибо")

    owner = Device(browser, OWNER_ID)
    owner.open()
    owner.page.wait_for_function("document.getElementById('owner-stats-btn').style.display !== 'none'", timeout=15000)
    owner.page.evaluate("openOwnerStats()")
    expect(owner.page.locator("#owner-stats-body")).to_contain_text("Удержание D7")


def test_rejected_write_is_kept_not_dropped(browser):
    uid = 2_300_000_000 + uuid.uuid4().int % 10**8
    d = Device(browser, uid)
    d.open()
    d.page.wait_for_function("getOutbox().length === 0")
    d.page.evaluate("apiCall('POST', '/api/tasks', {id: 'bad', text: 'x', prio: 'm', repeat_rule: 'hourly'})")
    d.page.wait_for_function("getFailedWrites().length === 1", timeout=15000)
    assert d.outbox_len() == 0
    assert d.page.evaluate("getFailedWrites()[0].status") == 422


def test_new_task_ids_are_unique(browser):
    uid = 2_400_000_000 + uuid.uuid4().int % 10**8
    d = Device(browser, uid)
    d.open()
    for i in range(3):
        d.add_task(f"Задача {i}")
    ids = d.page.evaluate("state.tasks.map(t => t.id)")
    assert len(set(ids)) == 3 and all(i.startswith("t_") for i in ids)


def test_rest_timer_starts_after_set_today(browser):
    d = Device(browser, 2_500_000_000 + uuid.uuid4().int % 10**8)
    d.open()
    d.page.evaluate("go('workout')")
    d.page.evaluate("""persistWorkoutEntry({client_id:'w_test', date: todayStr(), muscle: currentMuscle,
        exercise:'Жим штанги лёжа', sets:1, reps:8, weight:60})""")
    d.page.wait_for_function("document.getElementById('rest-timer').classList.contains('open')")
    value = d.page.text_content("#rest-timer-value")
    assert value.startswith("1:") or value.startswith("0:")
    d.page.click("#rest-timer .rest-timer-btn.close")
    d.page.wait_for_function("!document.getElementById('rest-timer').classList.contains('open')")


def test_recent_food_appears_in_quick_picks(browser):
    d = Device(browser, 2_600_000_000 + uuid.uuid4().int % 10**8)
    d.open()
    d.page.evaluate("go('food')")
    d.page.evaluate("""openFoodItem({food_id:'x1', food_name:'Сырники домашние', calories:220, protein:12, fat:10, carbs:20,
        serving_desc:'на 100г'}, 150, 'завтрак')""")
    d.page.evaluate("confirmAddFood()")
    d.page.wait_for_function("document.querySelector('.food-quick-pick.recent')")
    assert "Сырники" in d.page.text_content(".food-quick-pick.recent")
    d.page.click(".food-quick-pick.recent")
    assert abs(d.page.evaluate("selectedFood.calories") - 220) < 0.6


def test_custom_dish_saved_as_favorite_with_per100_macros(browser):
    d = Device(browser, 3_100_000_000 + uuid.uuid4().int % 10**8)
    d.open()
    d.page.evaluate("go('food')")
    d.page.click(".food-dish-add")
    d.page.fill("#dish-name", "Курица с рисом")
    rows = d.page.locator("#dish-ingredients .dish-row")
    rows.nth(0).locator(".dish-food").fill("Куриная грудка")
    rows.nth(0).locator(".dish-grams").fill("200")
    rows.nth(1).locator(".dish-food").fill("Рис варёный")
    rows.nth(1).locator(".dish-grams").fill("200")
    d.page.click("#modal-dish .btn-primary")
    fav = d.page.evaluate("state.foodFavorites.find(f => f.food_name === 'Курица с рисом')")
    # (165*2 + 130*2) / 400 * 100 = 147.5
    assert abs(fav["calories"] - 147.5) < 0.2 and fav["food_id"].startswith("dish:")
    d.page.wait_for_function("getOutbox().length === 0", timeout=15000)
    assert "Курица с рисом" in d.page.text_content("#food-favorites-list")


def test_copy_food_from_other_day(browser):
    d = Device(browser, 3_200_000_000 + uuid.uuid4().int % 10**8)
    d.open()
    d.page.evaluate("go('food')")
    d.page.evaluate("""state.foodLog.push({id:'old1', client_id:'old1', date:'2026-01-05', meal_type:'обед',
        food_name:'Борщ', calories:180, protein:8, fat:6, carbs:20, amount:300}); saveStateLocal();""")
    d.page.click(".food-copy-day-btn")
    d.page.click(".copy-day-item")
    d.page.wait_for_function("state.foodLog.some(e => e.date === currentFoodDate && e.food_name === 'Борщ')")


def test_home_day_plan_reflects_food_and_workout(browser):
    d = Device(browser, 3_400_000_000 + uuid.uuid4().int % 10**8)
    d.open()
    d.page.evaluate("go('home')")
    assert d.page.locator("#home-nudge-list .home-nudge.done").count() <= 1
    d.page.evaluate("""state.foodLog.push({id:'f1',client_id:'f1',date:todayStr(),meal_type:'завтрак',food_name:'Каша',calories:2100,protein:5,fat:5,carbs:50,amount:300});
        state.workouts.push({client_id:'w1',date:todayStr(),muscle:'Грудь + Трицепс',exercise:'Жим',sets:1,reps:5,weight:50});
        saveStateLocal(); go('home');""")
    titles = d.page.evaluate("[...document.querySelectorAll('#home-nudge-list .home-nudge.done .home-nudge-title')].map(e=>e.textContent)")
    assert "Питание" in titles and "Тренировка" in titles
    assert d.page.text_content("#home-day-score").startswith(str(len(titles)))
