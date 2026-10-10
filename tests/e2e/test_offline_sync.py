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
RAILWAY = "https://planner-backend-three.vercel.app"
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

    def __init__(self, browser, user_id: int, welcome: bool = False):
        self.api_down = False
        self.ctx = browser.new_context(viewport={"width": 390, "height": 844}, service_workers="allow",
                                       device_scale_factor=float(os.environ.get("SHOT_DSF", "1")))
        self.ctx.route("https://telegram.org/js/telegram-web-app.js",
                       lambda r: r.fulfill(content_type="application/javascript", body=telegram_stub(user_id)))
        self.ctx.route(f"{RAILWAY}/**", self._proxy_api)
        # внешние CDN в CI/песочнице могут быть недоступны — отдаём локальные копии
        self.ctx.route("https://cdnjs.cloudflare.com/**",
                       lambda r: r.fulfill(content_type="application/javascript", path=str(CHART_JS)))
        fonts_dir = os.environ.get("LOCAL_FONTS_DIR")  # для скриншотов: реальные шрифты без сети
        if fonts_dir:
            css = Path(fonts_dir, "local-fonts.css").read_text()
            self.ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(content_type="text/css", body=css))
            self.ctx.route("https://fontfiles.local/**", lambda r: r.fulfill(
                content_type="font/woff2",
                path=str(Path(fonts_dir, "fonts/node_modules/@fontsource", r.request.url.split("fontfiles.local/", 1)[1]))))
        else:
            self.ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(content_type="text/css", body=""))
        self.ctx.route("https://world.openfoodfacts.org/**", lambda r: r.abort())
        self.ctx.add_init_script(f"localStorage.setItem('planner_profile_skipped_{user_id}','1')")
        if not welcome:
            self.ctx.add_init_script(f"localStorage.setItem('planner_welcome_done_{user_id}','1')")
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
    # отклонённая операция ушла из очереди (другие фоновые записи могут ещё отправляться)
    assert not d.page.evaluate("getOutbox().some(i => i.body && i.body.id === 'bad')")
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


def test_set_with_rpe_and_note_syncs(browser):
    uid = 3_500_000_000 + uuid.uuid4().int % 10**8
    d = Device(browser, uid)
    d.open()
    d.page.evaluate("go('workout')")
    d.page.evaluate("openAddSetForExercise('Жим штанги лёжа')")
    d.page.fill("#m-sets", "1"); d.page.fill("#m-reps", "5"); d.page.fill("#m-weight", "100")
    d.page.click("#m-rpe button[data-rpe='8']")
    d.page.fill("#m-set-note", "пауза внизу")
    d.page.click("#modal-set .btn-primary")
    d.page.wait_for_function("getOutbox().length === 0", timeout=15000)
    laptop = Device(browser, uid)
    laptop.open()
    laptop.page.wait_for_function("state.workouts.some(w => w.rpe === 8 && w.note === 'пауза внизу')", timeout=15000)


def _open_workout(d):
    d.page.evaluate("go('workout')")
    d.page.wait_for_selector(".lg-card.active")


def test_quick_log_with_steppers(browser):
    d = Device(browser, 3_600_000_000 + uuid.uuid4().int % 10**8)
    d.open(); _open_workout(d)
    ex = d.page.text_content(".lg-card.active .lg-name")
    start = d.page.evaluate(f"workoutDrafts[{json.dumps(ex)}].weight")
    d.page.click(".lg-card.active .lg-step:nth-child(1) button[aria-label='Вес больше']")
    d.page.click(".lg-card.active .lg-step:nth-child(1) button[aria-label='Вес больше']")
    d.page.click(".lg-card.active .lg-log")
    d.page.wait_for_function(f"state.workouts.some(w => w.exercise === {json.dumps(ex)})")
    w = d.page.evaluate(f"state.workouts.find(w => w.exercise === {json.dumps(ex)})")
    assert w["sets"] == 1 and abs(w["weight"] - (start + 5)) < 0.01
    assert "подход 2" in d.page.text_content(".lg-card.active .lg-log")


def test_typed_value_is_used_when_tapping_log_immediately(browser):
    d = Device(browser, 3_700_000_000 + uuid.uuid4().int % 10**8)
    d.open(); _open_workout(d)
    ex = d.page.text_content(".lg-card.active .lg-name")
    d.page.fill(".lg-card.active .lg-step:nth-child(1) .lg-input", "62,5")
    d.page.fill(".lg-card.active .lg-step:nth-child(2) .lg-input", "11")
    d.page.click(".lg-card.active .lg-log")
    d.page.wait_for_function(f"state.workouts.some(w => w.exercise === {json.dumps(ex)})")
    w = d.page.evaluate(f"state.workouts.find(w => w.exercise === {json.dumps(ex)})")
    assert w["weight"] == 62.5 and w["reps"] == 11


def test_edit_set_keeps_identity_and_syncs(browser):
    uid = 3_800_000_000 + uuid.uuid4().int % 10**8
    d = Device(browser, uid)
    d.open(); _open_workout(d)
    d.page.click(".lg-card.active .lg-log")
    d.page.wait_for_function("getOutbox().length === 0 && state.workouts.length === 1", timeout=15000)
    cid = d.page.evaluate("state.workouts[0].client_id")
    d.page.click(".lg-set-main")
    d.page.click(".lg-set-actions button:has-text('Изменить')")
    d.page.fill("#m-reps", "3")
    d.page.click("#modal-set .btn-primary")
    d.page.wait_for_function("state.workouts.length === 1 && state.workouts[0].reps === 3")
    assert d.page.evaluate("state.workouts[0].client_id") == cid
    d.page.wait_for_function("getOutbox().length === 0", timeout=15000)
    laptop = Device(browser, uid); laptop.open()
    laptop.page.wait_for_function("state.workouts.length === 1 && state.workouts[0].reps === 3", timeout=15000)


def test_delete_set_from_row_menu(browser):
    d = Device(browser, 3_900_000_000 + uuid.uuid4().int % 10**8)
    d.open(); _open_workout(d)
    d.page.click(".lg-card.active .lg-log")
    d.page.wait_for_function("state.workouts.length === 1")
    d.page.click(".lg-set-main")
    d.page.click(".lg-set-actions .danger")
    d.page.wait_for_function("state.workouts.length === 0")


def test_progression_suggestion_after_easy_session(browser):
    d = Device(browser, 4_000_000_000 + uuid.uuid4().int % 10**8)
    d.open()
    d.page.evaluate("""(() => {
      const ex = getExerciseList(currentMuscle)[0];
      const y = new Date(Date.now() - 86400000); const ds = localDateISO(y);
      for (let i = 0; i < 3; i++) state.workouts.push({client_id:'h'+i, date: ds, muscle: currentMuscle, exercise: ex, sets:1, reps:8, weight:80, rpe:8});
      saveStateLocal(); window._ex = ex;
    })()""")
    _open_workout(d)
    ex = d.page.evaluate("window._ex")
    d.page.evaluate(f"activateExercise({json.dumps(ex)})")
    assert d.page.evaluate(f"workoutDrafts[{json.dumps(ex)}].weight") == 82.5
    assert "82,5" in d.page.text_content(".lg-card.active .lg-hint")


def test_only_first_beating_set_is_record(browser):
    d = Device(browser, 4_300_000_000 + uuid.uuid4().int % 10**8)
    d.open()
    d.page.evaluate("""(() => {
      const ex = getExerciseList(currentMuscle)[0];
      const y = localDateISO(new Date(Date.now() - 2*86400000));
      state.workouts.push({client_id:'old', date: y, muscle: currentMuscle, exercise: ex, sets:1, reps:8, weight:80});
      saveStateLocal();
    })()""")
    _open_workout(d)
    d.page.click(".lg-card.active button[aria-label='Вес больше']")  # 82,5 > 80
    d.page.click(".lg-card.active .lg-log")
    d.page.click(".lg-card.active .lg-log")
    d.page.wait_for_function("document.querySelectorAll('.lg-card.active .lg-set').length === 2")
    assert d.page.locator(".lg-card.active .lg-set.pr").count() == 1


def test_first_run_onboarding_sets_home_modules(browser):
    uid = 4_400_000_000 + uuid.uuid4().int % 10**8
    d = Device(browser, uid, welcome=True)
    d.open()
    d.page.wait_for_selector("#onboarding.open")
    d.page.click("[data-onb='1'] .onb-cta")
    # по умолчанию «Добавки» выключены; снимаем «Вес» — остаются задачи, питание, тренировки
    d.page.click(".onb-mod:has-text('Вес и замеры')")
    assert d.page.text_content("#onb-hint").startswith("Выбрано: 3")
    d.page.click("#onb-mods-next")
    assert d.page.locator(".onb-final-row").count() == 3
    d.page.click("#onb-final-later")
    d.page.wait_for_selector("#onboarding:not(.open)", state="attached")
    titles = d.page.eval_on_selector_all("#home-nudge-list .home-nudge-title", "els => els.map(e => e.textContent)")
    assert sorted(titles) == ["Задачи", "Питание", "Тренировка"]
    # повторное открытие — без онбординга
    d.page.reload(); d.page.wait_for_timeout(1500)
    assert not d.page.is_visible("#onboarding.open")


def test_onboarding_skipped_when_account_has_data(browser):
    uid = 4_500_000_000 + uuid.uuid4().int % 10**8
    a = Device(browser, uid)
    a.open()
    a.page.evaluate("state.tasks.push({id:'x1', text:'Есть данные', prio:'m', dl: todayStr(), done:false}); saveState()")
    a.page.wait_for_timeout(1500)
    b = Device(browser, uid, welcome=True)
    b.open(); b.page.wait_for_timeout(2500)
    assert not b.page.is_visible("#onboarding.open")


def test_empty_tasks_offer_quick_start(browser):
    d = Device(browser, 4_600_000_000 + uuid.uuid4().int % 10**8)
    d.open()
    d.page.evaluate("go('tasks')")
    d.page.click(".empty-chip:has-text('Купить продукты')")
    d.page.wait_for_selector("#modal-task.open")
    assert d.page.input_value("#m-task-text") == "Купить продукты"


def test_modules_editor_from_more_menu(browser):
    d = Device(browser, 4_700_000_000 + uuid.uuid4().int % 10**8)
    d.open()
    d.page.evaluate("openModulesEditor()")
    d.page.click(".onb-mod:has-text('Тренировки')")
    d.page.click("#onb-mods-next")
    assert not d.page.is_visible("#onboarding.open")
    titles = d.page.eval_on_selector_all("#home-nudge-list .home-nudge-title", "els => els.map(e => e.textContent)")
    assert "Тренировка" not in titles and "Задачи" in titles


# ── Полностью офлайн: нет ни сервера, ни сети (самолётный режим) ──
def go_offline(d: Device):
    d.api_down = True
    d.ctx.set_offline(True)


def go_online(d: Device):
    d.ctx.set_offline(False)
    d.api_down = False


def wait_sw(d: Device):
    d.page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.controller !== null", timeout=15000)
    # оболочка должна быть в кеше до отключения сети
    d.page.wait_for_function("""(async () => {
      const keys = await caches.keys();
      for (const k of keys) { if (await (await caches.open(k)).match('./index.html')) return true; }
      return false; })()""", timeout=15000)


def counts(d: Device):
    return d.page.evaluate("""({
      tasks: state.tasks.map(t => t.text + (t.done ? ':done' : '')).sort(),
      food: state.foodLog.filter(e => e.date === todayStr()).length,
      sets: state.workouts.filter(w => w.date === todayStr()).length,
      weight: (state.bodyWeight.find(e => e.date === todayStr()) || {}).weight || null,
      supps: Object.keys(state.supps.checked).filter(k => k.startsWith(todayStr())).length
    })""")


def test_cold_start_and_full_day_offline(browser):
    uid = 4_800_000_000 + uuid.uuid4().int % 10**8
    tag = uuid.uuid4().hex[:4]
    phone = Device(browser, uid)
    errors = []
    phone.page.on("pageerror", lambda e: errors.append(str(e)))
    phone.open()
    phone.page.wait_for_function("getOutbox().length === 0")
    wait_sw(phone)

    # ── Самолётный режим, холодный запуск ──
    go_offline(phone)
    phone.page.reload()
    phone.page.wait_for_function("typeof state !== 'undefined' && typeof getOutbox === 'function'", timeout=15000)
    assert phone.page.is_visible("#s-home.active")

    # Задачи: три новых, одну закрыть, одну удалить
    for n in (1, 2, 3):
        phone.add_task(f"Офлайн-{tag}-{n}")
    phone.page.evaluate(f"toggleTask(state.tasks.find(t => t.text === 'Офлайн-{tag}-1').id)")
    phone.page.evaluate(f"deleteTask(state.tasks.find(t => t.text === 'Офлайн-{tag}-3').id)")

    # Питание: быстрый продукт из локальной базы
    phone.page.evaluate("go('food')")
    phone.page.click(".food-quick-pick >> nth=0")
    phone.page.wait_for_selector("#modal-food-add.open")
    phone.page.evaluate("confirmAddFood()")
    phone.page.wait_for_function("state.foodLog.filter(e => e.date === todayStr()).length === 1")

    # Тренировка: два подхода в одно касание
    phone.page.evaluate("go('workout')")
    phone.page.wait_for_selector(".lg-card.active")
    phone.page.click(".lg-card.active .lg-log")
    phone.page.click(".lg-card.active .lg-log")
    phone.page.wait_for_function("state.workouts.filter(w => w.date === todayStr()).length === 2")

    # Вес и добавки
    phone.page.evaluate("go('body'); openAddBody('weight')")
    phone.page.fill("#m-body-weight", "79.4")
    phone.page.click("#modal-body .btn-primary")
    phone.page.evaluate("go('supps')")
    phone.page.click("#supp-list .time-chip >> nth=0")

    # Графики и отчёт не падают без сети
    phone.page.evaluate("go('charts'); go('report'); go('home')")

    before = counts(phone)
    assert before == {
        "tasks": sorted([f"Офлайн-{tag}-1:done", f"Офлайн-{tag}-2"]),
        "food": 1, "sets": 2, "weight": 79.4, "supps": 1,
    }, before
    assert phone.outbox_len() > 0

    # ── Перезапуск всё ещё без сети: ничего не потерялось ──
    phone.page.reload()
    phone.page.wait_for_function("typeof state !== 'undefined' && typeof getOutbox === 'function'", timeout=15000)
    assert counts(phone) == before
    assert phone.outbox_len() > 0

    # ── Сеть вернулась: очередь уходит на сервер ──
    go_online(phone)
    phone.page.evaluate("syncNow()")
    phone.page.wait_for_function("getOutbox().length === 0", timeout=20000)
    assert phone.page.evaluate("getFailedWrites().length") == 0

    # ── Второе устройство видит тот же день ──
    laptop = Device(browser, uid)
    laptop.open()
    laptop.page.wait_for_function(f"state.tasks.some(t => t.text === 'Офлайн-{tag}-2')", timeout=15000)
    laptop.page.wait_for_function("state.workouts.filter(w => w.date === todayStr()).length === 2", timeout=15000)
    assert counts(laptop) == before
    assert not errors, errors

