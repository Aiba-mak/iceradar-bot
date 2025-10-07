import os
import ssl
from typing import Dict, Tuple, Optional, List
from datetime import datetime, timezone, timedelta

import asyncpg
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto,
)

# ========= ENV =========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")
assert BOT_TOKEN, "❌ BOT_TOKEN не найден в .env"
assert DB_URL, "❌ DATABASE_URL не найден в .env"

# ========= BOT / DP / ROUTER =========
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ========= DB POOL =========
pool: Optional[asyncpg.Pool] = None

def _make_ssl_context() -> ssl.SSLContext:
    base = os.path.dirname(__file__)
    for name in ("supabase-ca.crt", "prod-ca-2021.crt", "supabase-ca.pem"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            print(f"SSL: using CA file {name}")
            return ssl.create_default_context(cafile=path)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    print("SSL: verification DISABLED (dev fallback). Put supabase-ca.crt next to bot.py to enable verification.")
    return ctx

async def open_db_pool():
    global pool
    ssl_ctx = _make_ssl_context()
    pool = await asyncpg.create_pool(dsn=DB_URL, min_size=1, max_size=5, ssl=ssl_ctx)
    async with pool.acquire() as con:
        ver = await con.fetchval("select version()")
        print("DB pool ready ✅", ver)

        # PostGIS (если есть права)
        try:
            await con.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        except Exception:
            pass

        # app_user
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS app_user(
                id BIGSERIAL PRIMARY KEY,
                tg_user_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                lang TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_app_user_lang ON app_user(lang);
            """
        )

        # poi.created_at (если нет)
        await con.execute(
            """
            DO $$
            BEGIN
              IF to_regclass('public.poi') IS NOT NULL THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_name='poi' AND column_name='created_at'
                ) THEN
                  EXECUTE 'ALTER TABLE poi ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()';
                END IF;
              END IF;
            END
            $$;
            """
        )

        # poi_confirmation.created_at (если нет — добавить, заполнить, NOT NULL)
        await con.execute(
            """
            DO $$
            BEGIN
              IF to_regclass('public.poi_confirmation') IS NOT NULL THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_name='poi_confirmation' AND column_name='created_at'
                ) THEN
                  EXECUTE 'ALTER TABLE poi_confirmation ADD COLUMN created_at TIMESTAMPTZ DEFAULT NOW()';
                  EXECUTE 'UPDATE poi_confirmation SET created_at = NOW() WHERE created_at IS NULL';
                  EXECUTE 'ALTER TABLE poi_confirmation ALTER COLUMN created_at SET NOT NULL';
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname='uq_poi_confirmation_poi_user'
                ) THEN
                  EXECUTE 'CREATE UNIQUE INDEX uq_poi_confirmation_poi_user ON poi_confirmation(poi_id, user_id)';
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname='idx_poi_confirmation_created'
                ) THEN
                  EXECUTE 'CREATE INDEX idx_poi_confirmation_created ON poi_confirmation(created_at)';
                END IF;
              END IF;
            END
            $$;
            """
        )

        # Индекс по подтверждениям (для джойна)
        await con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_poi_confirmation_poi_id
            ON poi_confirmation(poi_id);
            """
        )

        # Таблица для фото
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS poi_photo (
                id BIGSERIAL PRIMARY KEY,
                poi_id BIGINT NOT NULL REFERENCES poi(id) ON DELETE CASCADE,
                file_id TEXT NOT NULL,
                user_id BIGINT REFERENCES app_user(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_poi_photo_poi_id ON poi_photo(poi_id);
            CREATE INDEX IF NOT EXISTS idx_poi_photo_poi_created ON poi_photo(poi_id, created_at DESC);
            """
        )

        # Подписки на геозоны (+ last_refreshed_at)
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id BIGSERIAL PRIMARY KEY,
                tg_user_id BIGINT NOT NULL,
                center geography(Point,4326) NOT NULL,
                radius_m DOUBLE PRECISION NOT NULL CHECK (radius_m > 0),
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='subscriptions' AND column_name='last_refreshed_at'
              ) THEN
                EXECUTE 'ALTER TABLE subscriptions ADD COLUMN last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()';
              END IF;
            END$$;
            CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(tg_user_id);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_active ON subscriptions(is_active);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_center ON subscriptions USING GIST (geography(center));
            CREATE INDEX IF NOT EXISTS idx_subscriptions_refresh ON subscriptions(last_refreshed_at);
            """
        )

async def close_db_pool():
    global pool
    if pool is not None:
        await pool.close()
        pool = None
        print("DB pool closed.")

dp.startup.register(open_db_pool)
dp.shutdown.register(close_db_pool)

# ========= HELPERS / CONSTANTS =========
CONFIRM_RADIUS_MI = 0.5              # радиус подтверждения
FRESH_LOCATION_MINUTES = 10          # "свежесть" локации
SUBSCRIPTION_TTL_HOURS = 12          # TTL для "живых" обновлений (для статического трекинга мы его продлеваем при новой локации)
ADD_LIMIT_COUNT = 2                  # лимит добавлений
ADD_LIMIT_WINDOW_H = 12              # окно лимита, часов

LANGS = {
    "ru": "Русский",
    "en": "English",
    "es": "Español (LatAm)",
    "ar": "العربية",
    "hi": "हिन्दी",
}
DEFAULT_LANG = "en"  # интерфейс по умолчанию — EN

# Короткий словарь строк интерфейса
STRINGS = {
    "start_intro": {
        "ru": (
            "Привет! Я помогу найти точки рядом и добавить свои.\n\n"
            "• Пришли локацию → выбери радиус 10/50/100 mi\n"
            "• Нажми «➕ Добавить точку», чтобы создать новую\n"
            "• Нажми «📡 Отслеживание», чтобы получать новые точки рядом\n\n"
        ),
        "en": (
            "Hi! I can find nearby spots and let you add your own.\n\n"
            "• Send your location → choose a radius 10/50/100 mi\n"
            "• Tap “➕ Add spot” to create a new one\n"
            "• Tap “📡 Tracking” to get nearby alerts\n\n"
        ),
        "es": (
            "¡Hola! Puedo buscar puntos cercanos y dejarte agregar los tuyos.\n\n"
            "• Envía tu ubicación → elige radio 10/50/100 mi\n"
            "• Toca “➕ Agregar punto” para crear uno nuevo\n"
            "• Toca “📡 Rastreo” para recibir alertas cercanas\n\n"
        ),
        "ar": (
            "مرحبًا! سأساعدك في العثور على النقاط القريبة وإضافة نقاطك.\n\n"
            "• أرسل موقعك → اختر نصف قطر 10/50/100 ميل\n"
            "• اضغط “➕ إضافة نقطة” لإنشاء نقطة جديدة\n"
            "• اضغط “📡 التتبع” لتلقي التنبيهات القريبة\n\n"
        ),
        "hi": (
            "नमस्ते! मैं पास के पॉइंट ढूँढूँगा और आप अपने जोड़ सकते हैं।\n\n"
            "• अपनी लोकेशन भेजें → 10/50/100 मील का रेडियस चुनें\n"
            "• “➕ पॉइंट जोड़ें” दबाएँ नया बनाने के लिए\n"
            "• “📡 ट्रैकिंग” दबाएँ पास के अलर्ट पाने के लिए\n\n"
        ),
    },
    "subscription_badge": {
        "ru": "Подписка",
        "en": "Subscription",
        "es": "Suscripción",
        "ar": "الاشتراك",
        "hi": "सदस्यता",
    },
    "already_subscribed": {
        "ru": "Отслеживание уже включено.",
        "en": "Tracking is already ON.",
        "es": "El rastreo ya está ACTIVADO.",
        "ar": "التتبع مُفعَّل بالفعل.",
        "hi": "ट्रैकिंग पहले से चालू है।",
    },
    "send_location_first": {
        "ru": "Сначала пришли текущую локацию:",
        "en": "Send your current location first:",
        "es": "Primero envía tu ubicación actual:",
        "ar": "أرسل موقعك الحالي أولاً:",
        "hi": "पहले अपनी वर्तमान लोकेशन भेजें:",
    },
    "location_saved": {
        "ru": "✅ Локация сохранена: <b>{lat:.5f}, {lon:.5f}</b>\nВыбери радиус:",
        "en": "✅ Location saved: <b>{lat:.5f}, {lon:.5f}</b>\nChoose a radius:",
        "es": "✅ Ubicación guardada: <b>{lat:.5f}, {lon:.5f}</b>\nElige un radio:",
        "ar": "✅ تم حفظ الموقع: <b>{lat:.5f}, {lon:.5f}</b>\nاختر نصف القطر:",
        "hi": "✅ लोकेशन सेव हो गई: <b>{lat:.5f}, {lon:.5f}</b>\nरेडियस चुनें:",
    },
    "no_points_in_radius": {
        "ru": "В радиусе {miles} mi активных точек не найдено (фильтр 12 часов).",
        "en": "No active points found within {miles} mi (12-hour filter).",
        "es": "No se encontraron puntos activos dentro de {miles} mi (filtro de 12 horas).",
        "ar": "لا توجد نقاط نشطة ضمن {miles} ميل (مرشح 12 ساعة).",
        "hi": "{miles} मील के भीतर कोई सक्रिय पॉइंट नहीं मिला (12-घंटे फ़िल्टर)।",
    },
    "back_to_start": {
        "ru": "↩️ Вернуться в начало: нажми /start",
        "en": "↩️ Back to start: type /start",
        "es": "↩️ Volver al inicio: escribe /start",
        "ar": "↩️ العودة للبداية: اكتب /start",
        "hi": "↩️ शुरू पर लौटें: /start लिखें",
    },
    "confirm_button": {
        "ru": "Подтвердить точку",
        "en": "Confirm point",
        "es": "Confirmar punto",
        "ar": "تأكيد النقطة",
        "hi": "पॉइंट कन्फर्म करें",
    },
    "choose_category": {
        "ru": "Выбери категорию:",
        "en": "Choose a category:",
        "es": "Elige una categoría:",
        "ar": "اختر فئة:",
        "hi": "श्रेणी चुनें:",
    },
    "category_wrong": {
        "ru": "Пожалуйста, выбери одну из двух категорий кнопками ниже:",
        "en": "Please choose one of the two categories using the buttons below:",
        "es": "Por favor elige una de las dos categorías con los botones abajo:",
        "ar": "رجاءً اختر إحدى الفئتين عبر الأزرار بالأسفل:",
        "hi": "कृपया नीचे दिए बटन से दो में से एक श्रेणी चुनें:",
    },
    "short_desc": {
        "ru": "Короткое описание? (можно пропустить)",
        "en": "Short description? (optional)",
        "es": "¿Descripción corta? (opcional)",
        "ar": "وصف قصير؟ (اختياري)",
        "hi": "छोटा विवरण? (वैकल्पिक)",
    },
    "send_photos": {
        "ru": "Отправляй одно или несколько фото подряд. Когда закончишь — нажми «Готово» (или «Пропустить»).",
        "en": "Send one or more photos. When done, press “Done” (or “Skip”).",
        "es": "Envía una o varias fotos. Cuando termines, pulsa “Listo” (o “Omitir”).",
        "ar": "أرسل صورة أو أكثر. عند الانتهاء اضغط \"تم\" (أو \"تخطي\").",
        "hi": "एक या अधिक फोटो भेजें। पूरा होने पर “हो गया” (या “स्किप”) दबाएँ.",
    },
    "photo_saved": {
        "ru": "📸 Фото сохранено ({n}). Можно отправить ещё или нажать «Готово».",
        "en": "📸 Photo saved ({n}). You can send more or press “Done”.",
        "es": "📸 Foto guardada ({n}). Puedes enviar más o pulsar “Listo”.",
        "ar": "📸 تم حفظ الصورة ({n}). يمكنك إرسال المزيد أو الضغط على \"تم\".",
        "hi": "📸 फोटो सेव हुई ({n}). और भेजें या “हो गया” दबाएँ.",
    },
    "point_added": {
        "ru": "✅ Точка добавлена{photo}: <b>{category}</b>\n",
        "en": "✅ Point added{photo}: <b>{category}</b>\n",
        "es": "✅ Punto agregado{photo}: <b>{category}</b>\n",
        "ar": "✅ تم إضافة نقطة{photo}: <b>{category}</b>\n",
        "hi": "✅ पॉइंट जोड़ा गया{photo}: <b>{category}</b>\n",
    },
    "added_at": {
        "ru": "⏱ добавлено: <b>{ts}</b>\n",
        "en": "⏱ added: <b>{ts}</b>\n",
        "es": "⏱ agregado: <b>{ts}</b>\n",
        "ar": "⏱ تمت الإضافة: <b>{ts}</b>\n",
        "hi": "⏱ जोड़ा गया: <b>{ts}</b>\n",
    },
    "limit_reached": {
        "ru": "Вы достигли лимита: не более 2 точек за 12 часов.",
        "en": "Limit reached: at most 2 points per 12 hours.",
        "es": "Límite alcanzado: máximo 2 puntos cada 12 horas.",
        "ar": "تم بلوغ الحد: بحد أقصى نقطتان كل 12 ساعة.",
        "hi": "सीमा पूरी: 12 घंटे में अधिकतम 2 पॉइंट।",
    },
    "subscribe_set": {
        "ru": "🔔 Подписка оформлена: {miles} mi от текущей локации на 12 часов. Продлится при отправке новой локации.",
        "en": "🔔 Subscription set: {miles} mi around your current location for 12 hours. It prolongs when you send a new location.",
        "es": "🔔 Suscripción creada: {miles} mi alrededor de tu ubicación por 12 horas. Se renueva al enviar nueva ubicación.",
        "ar": "🔔 تم إعداد الاشتراك: ضمن {miles} ميل حول موقعك الحالي لمدة 12 ساعة. يتم التمديد عند إرسال موقع جديد.",
        "hi": "🔔 सदस्यता सेट: आपकी वर्तमान लोकेशन के {miles} मील तक 12 घंटे के लिए। नई लोकेशन भेजने पर बढ़ेगी।",
    },
    "need_active_sub": {
        "ru": "Чтобы делиться Live-локацией, сначала включи отслеживание (кнопка «📡 Отслеживание»).",
        "en": "To share a live location, turn Tracking ON first (tap “📡 Tracking”).",
        "es": "Para compartir ubicación en vivo, activa el rastreo primero (toca “📡 Rastreo”).",
        "ar": "لمشاركة الموقع المباشر، شغّل التتبع أولًا (اضغط “📡 التتبع”).",
        "hi": "Live location के लिए पहले Tracking चालू करें (“📡 ट्रैकिंग”).",
    },
    "live_howto": {
        "ru": (
            "🟢 Как включить Live-локацию:\n\n"
            "1) Нажми скрепку (📎) → Местоположение.\n"
            "2) Выбери ‘Передавать местоположение в реальном времени’.\n"
            "3) Поставь срок: 1 час или 8 часов.\n\n"
            "Пока Live включена, мы автоматически обновляем центр отслеживания."
        ),
        "en": (
            "🟢 How to enable Live Location:\n\n"
            "1) Tap paperclip (📎) → Location.\n"
            "2) Choose ‘Share live location’.\n"
            "3) Set duration: 1 hour or 8 hours.\n\n"
            "While live is on, we auto-refresh the tracking center."
        ),
        "es": (
            "🟢 Cómo activar Ubicación en vivo:\n\n"
            "1) Toca el clip (📎) → Ubicación.\n"
            "2) Elige ‘Compartir ubicación en vivo’.\n"
            "3) Pon duración: 1 hora u 8 horas.\n\n"
            "Mientras esté activa, actualizamos el centro de rastreo automáticamente."
        ),
        "ar": (
            "🟢 طريقة تفعيل الموقع المباشر:\n\n"
            "1) اضغط مشبك الورق (📎) → الموقع.\n"
            "2) اختر ‘مشاركة الموقع المباشر’.\n"
            "3) عيّن المدة: ساعة أو 8 ساعات.\n\n"
            "طالما أن الـLive مفعّل سنحدّث مركز التتبع تلقائيًا."
        ),
        "hi": (
            "🟢 लाइव लोकेशन कैसे चालू करें:\n\n"
            "1) पेपरक्लिप (📎) → लोकेशन।\n"
            "2) ‘लाइव लोकेशन शेयर करें’ चुनें।\n"
            "3) अवधि सेट करें: 1 घंटा या 8 घंटे।\n\n"
            "जब तक लाइव चालू है, ट्रैकिंग सेंटर ऑटो-रिफ्रेश होता रहेगा."
        ),
    },
    "live_soon_off": {
        "ru": "⏰ Live-локация скоро отключится (≈10 мин). Чтобы продолжить авто-обновление, включи Live снова:\n📎 Скрепка → Местоположение → Передавать местоположение в реальном времени.",
        "en": "⏰ Live location will end in ≈10 min. To keep auto-refreshing, enable it again:\n📎 Paperclip → Location → Share live location.",
        "es": "⏰ La ubicación en vivo terminará en ≈10 min. Para seguir auto-actualizando, actívala otra vez:\n📎 Clip → Ubicación → Compartir ubicación en vivo.",
        "ar": "⏰ سيتوقف الموقع المباشر بعد ≈10 دقائق. للحفاظ على التحديث التلقائي فعّله مجددًا:\n📎 مشبك → الموقع → مشاركة الموقع المباشر.",
        "hi": "⏰ लाइव लोकेशन ~10 मिनट में बंद होगी। ऑटो-रिफ्रेश जारी रखने के लिए फिर से चालू करें:\n📎 पेपरक्लिप → लोकेशन → लाइव लोकेशन शेयर।",
    },
    "need_fresh_loc": {
        "ru": "Локация устарела (нужно обновление ≤ {mins} мин). Пришлите текущую локацию кнопкой ниже и повторите подтверждение.",
        "en": "Location is stale (needs update ≤ {mins} min). Send a fresh location below and confirm again.",
        "es": "La ubicación está desactualizada (necesita actualización ≤ {mins} min). Envía una ubicación nueva abajo y confirma de nuevo.",
        "ar": "الموقع قديم (يحتاج تحديث خلال ≤ {mins} دقيقة). أرسل موقعًا جديدًا أدناه ثم أكد مرة أخرى.",
        "hi": "लोकेशन पुरानी है (≤ {mins} मिनट में अपडेट चाहिए)। नीचे नई लोकेशन भेजें और फिर कन्फर्म करें।",
    },
    "too_far_to_confirm": {
        "ru": "Вы не можете подтвердить находясь далеко от места (> 0.5 mi от точки).",
        "en": "You are too far to confirm (> 0.5 mi from the point).",
        "es": "Estás demasiado lejos para confirmar (> 0.5 mi del punto).",
        "ar": "أنت بعيد جدًا للتأكيد (> 0.5 ميل عن النقطة).",
        "hi": "आप बहुत दूर हैं, कन्फर्म नहीं कर सकते (> 0.5 मील)।",
    },
    "tracking_on": {
        "ru": "📡 Отслеживание 🟢 ВКЛЮЧЕНО",
        "en": "📡 Tracking 🟢 ON",
        "es": "📡 Rastreo 🟢 ACTIVADO",
        "ar": "📡 التتبع 🟢 قيد التشغيل",
        "hi": "📡 ट्रैकिंग 🟢 चालू",
    },
    "tracking_off": {
        "ru": "📡 Отслеживание 🔴 ВЫКЛЮЧЕНО",
        "en": "📡 Tracking 🔴 OFF",
        "es": "📡 Rastreo 🔴 DESACTIVADO",
        "ar": "📡 التتبع 🔴 متوقف",
        "hi": "📡 ट्रैकिंग 🔴 बंद",
    },
    "pick_tracking_radius": {
        "ru": "Выбери радиус отслеживания:",
        "en": "Choose tracking radius:",
        "es": "Elige el radio de rastreo:",
        "ar": "اختر نصف قطر التتبع:",
        "hi": "ट्रैकिंग रेडियस चुनें:",
    },
}

async def get_user_lang(tg_user_id: int) -> str:
    assert pool is not None
    async with pool.acquire() as con:
        lang = await con.fetchval("SELECT lang FROM app_user WHERE tg_user_id=$1", tg_user_id)
    return lang if lang in LANGS else DEFAULT_LANG

async def set_user_lang(tg_user_id: int, lang: str):
    if lang not in LANGS:
        lang = DEFAULT_LANG
    assert pool is not None
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO app_user(tg_user_id, lang)
            VALUES ($1, $2)
            ON CONFLICT (tg_user_id) DO UPDATE SET lang=EXCLUDED.lang
            """,
            tg_user_id, lang
        )

def L(lang: str, key: str, **kwargs) -> str:
    text = STRINGS.get(key, {}).get(lang) or STRINGS.get(key, {}).get(DEFAULT_LANG) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

# UI helpers

def start_kb(lang: str, tracking_active: bool):
    send_loc = ("📍 Отправить локацию" if lang=="ru" else
                ("📍 Send location" if lang=="en" else
                 ("📍 Enviar ubicación" if lang=="es" else
                  ("📍 أرسل الموقع" if lang=="ar" else "📍 लोकेशन भेजें"))))
    add_spot = ("➕ Добавить точку" if lang=="ru" else
                ("➕ Add spot" if lang=="en" else
                 ("➕ Agregar punto" if lang=="es" else
                  ("➕ إضافة نقطة" if lang=="ar" else "➕ पॉइंट जोड़ें"))))
    tracking_base = ("📡 Отслеживание" if lang=="ru" else
                     ("📡 Tracking" if lang=="en" else
                      ("📡 Rastreo" if lang=="es" else
                       ("📡 التتبع" if lang=="ar" else "📡 ट्रैकिंग"))))
    tracking = f"{tracking_base} {'🟢' if tracking_active else '🔴'}"
    lang_label = ("Язык" if lang=="ru" else
                  ("Language" if lang=="en" else
                   ("Idioma" if lang=="es" else
                    ("اللغة" if lang=="ar" else "भाषा"))))
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=send_loc, request_location=True)],
            [KeyboardButton(text=add_spot), KeyboardButton(text=tracking)],
            [KeyboardButton(text=lang_label)],
        ],
        resize_keyboard=True
    )

def home_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="/start")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def location_request_kb(lang: str):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=("📍 Отправить локацию" if lang=="ru" else
                                        ("📍 Send location" if lang=="en" else
                                         ("📍 Enviar ubicación" if lang=="es" else
                                          ("📍 أرسل الموقع" if lang=="ar" else "📍 लोकेशन भेजें"))
                                        )), request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def radius_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="10 mi", callback_data="r:10"),
        InlineKeyboardButton(text="50 mi", callback_data="r:50"),
        InlineKeyboardButton(text="100 mi", callback_data="r:100"),
    ]])

def track_radius_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="10 mi", callback_data="track:10"),
        InlineKeyboardButton(text="50 mi", callback_data="track:50"),
        InlineKeyboardButton(text="100 mi", callback_data="track:100"),
    ]])

def sub_radius_kb():  # оставлено для обратной совместимости (не используется напрямую пользователем)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="10 mi", callback_data="sub:10"),
        InlineKeyboardButton(text="50 mi", callback_data="sub:50"),
        InlineKeyboardButton(text="100 mi", callback_data="sub:100"),
    ]])

def confirm_kb(poi_id: int, lang: str) -> InlineKeyboardMarkup:
    label = STRINGS["confirm_button"].get(lang, STRINGS["confirm_button"]["en"])
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"cf:{poi_id}")]]
    )

# --- категории (нейтральные) и клавиатуры ---
CATEGORIES = ("🚧 Дорожное перекрытие", "⚠️ Общественное предупреждение")

# ===== /lang =====

def lang_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🇷🇺 Русский", callback_data="lang:ru"),
         InlineKeyboardButton(text=f"🇺🇸 English", callback_data="lang:en")],
        [InlineKeyboardButton(text=f"🇪🇸 Español (LatAm)", callback_data="lang:es"),
         InlineKeyboardButton(text=f"🇸🇦 العربية", callback_data="lang:ar")],
        [InlineKeyboardButton(text=f"🇮🇳 हिन्दी", callback_data="lang:hi")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(F.text.lower().in_({"/lang", "language", "язык", "idioma", "اللغة", "भाषा"}))
async def on_lang(m: Message):
    await m.answer("Select language / Выберите язык:", reply_markup=lang_kb())

@router.callback_query(F.data.startswith("lang:"))
async def on_lang_pick(cq: CallbackQuery):
    lang = cq.data.split(":", 1)[1]
    await set_user_lang(cq.from_user.id, lang)
    tracking_active = await has_active_subscription(cq.from_user.id)
    badge = await get_subscription_badge(cq.from_user.id)
    await cq.message.answer(
        L(lang, "start_intro") + badge,
        reply_markup=start_kb(lang, tracking_active)
    )
    await cq.answer()

# ===== simple helpers =====

def fmt_ts(ts: Optional[datetime]) -> str:
    if ts is None:
        return "-"
    try:
        return ts.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts.strftime("%Y-%m-%d %H:%M")

async def ensure_user(tg_user_id: int, username: Optional[str]) -> int:
    assert pool is not None
    async with pool.acquire() as con:
        await con.execute(
            """
            insert into app_user(tg_user_id, username)
            values ($1, $2)
            on conflict (tg_user_id) do update set username = excluded.username
            """,
            tg_user_id, username
        )
        row = await con.fetchrow("select id from app_user where tg_user_id=$1", tg_user_id)
        return int(row["id"])

# последняя локация пользователя + время получения
last_loc: Dict[int, Tuple[float, float, datetime]] = {}
from asyncio import create_task, Task, sleep
live_reminders: Dict[Tuple[int,int], Task] = {}

def miles_to_meters(mi: float) -> float:
    return mi * 1609.344

def is_location_fresh(uid: int) -> bool:
    if uid not in last_loc:
        return False
    _, _, ts = last_loc[uid]
    age = datetime.now(timezone.utc) - ts
    return age <= timedelta(minutes=FRESH_LOCATION_MINUTES)

# ========= FSM =========
class AddPoi(StatesGroup):
    waiting_location = State()
    waiting_category = State()
    waiting_description = State()
    waiting_photo = State()

# ========= HANDLERS =========
async def get_subscription_badge(uid: int) -> str:
    assert pool is not None
    async with pool.acquire() as con:
        r = await con.fetchrow(
            """
            SELECT radius_m, is_active, last_refreshed_at
            FROM subscriptions
            WHERE tg_user_id=$1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            uid,
        )
    if not r:
        return "🔴"
    is_active = bool(r["is_active"])
    miles = (r["radius_m"] or 0.0) / 1609.344
    until = (r["last_refreshed_at"] or datetime.now(timezone.utc)) + timedelta(hours=SUBSCRIPTION_TTL_HOURS)
    badge = "🟢" if is_active else "🔴"
    return f"{badge} • {miles:.0f} mi • {fmt_ts(until)}"

async def has_active_subscription(tg_user_id: int) -> bool:
    assert pool is not None
    async with pool.acquire() as con:
        ok = await con.fetchval(
            """
            SELECT EXISTS (
              SELECT 1 FROM subscriptions
              WHERE tg_user_id=$1 AND is_active
                AND last_refreshed_at >= NOW() - INTERVAL '30 days'
            )
            """,
            tg_user_id,
        )
    return bool(ok)

async def schedule_live_reminder(user_id: int, message_id: int, expire_at: datetime):
    key = (user_id, message_id)
    if key in live_reminders:
        try:
            live_reminders[key].cancel()
        except Exception:
            pass
    remind_at = expire_at - timedelta(minutes=10)
    delay = (remind_at - datetime.now(timezone.utc)).total_seconds()
    if delay <= 0:
        delay = 1

    async def _job():
        try:
            lang = await get_user_lang(user_id)
            await sleep(delay)
            await bot.send_message(
                chat_id=user_id,
                text=L(lang, "live_soon_off")
            )
        except Exception:
            pass
        finally:
            live_reminders.pop(key, None)
    live_reminders[key] = create_task(_job())

@router.message(CommandStart())
async def on_start(m: Message):
    lang = await get_user_lang(m.from_user.id)
    badge = await get_subscription_badge(m.from_user.id)
    tracking_active = await has_active_subscription(m.from_user.id)
    await m.answer(
        L(lang, "start_intro") + badge,
        reply_markup=start_kb(lang, tracking_active)
    )

# Сохранить локацию (фиксируем дубликат вывода)
@router.message(StateFilter(None), F.location)
async def on_location(m: Message):
    # live-локация — отдельный хэндлер
    if getattr(m.location, 'live_period', None):
        return await on_live_location_start(m)

    lat, lon = m.location.latitude, m.location.longitude
    last_loc[m.from_user.id] = (lat, lon, datetime.now(timezone.utc))

    # если есть активное отслеживание — обновляем центр и продлеваем "свежесть"
    assert pool is not None
    async with pool.acquire() as con:
        await con.execute(
            """
            UPDATE subscriptions
            SET center = ST_MakePoint($2,$3)::geography,
                last_refreshed_at = NOW(),
                is_active = TRUE
            WHERE tg_user_id = $1
            """,
            m.from_user.id, float(lon), float(lat)
        )

    lang = await get_user_lang(m.from_user.id)
    await m.answer(
        L(lang, "location_saved", lat=lat, lon=lon),
        reply_markup=radius_kb()
    )

# Поиск ближайших точек (с фото-альбомом, фильтр 12ч)
@router.callback_query(F.data.startswith("r:"))
async def on_radius(cq: CallbackQuery):
    uid = cq.from_user.id
    lang = await get_user_lang(uid)
    if uid not in last_loc:
        await cq.message.answer(L(lang, "send_location_first"),
                                reply_markup=start_kb(lang, await has_active_subscription(uid)))
        await cq.answer()
        return

    miles = int(cq.data.split(":")[1])
    lat, lon, _ = last_loc[uid]
    radius_m = miles_to_meters(miles)

    assert pool is not None
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT
                p.id, p.title, p.category, p.description,
                ST_Y(p.geom::geometry) AS lat,
                ST_X(p.geom::geometry) AS lon,
                ST_Distance(p.geom, ST_MakePoint($1,$2)::geography) AS dist_m,
                COUNT(pc.poi_id) AS confirmations,
                MAX(pc.created_at) AS last_conf,
                p.created_at AS created_at,
                ph.photos AS photos
            FROM poi p
            LEFT JOIN poi_confirmation pc ON pc.poi_id = p.id
            LEFT JOIN LATERAL (
              SELECT array_agg(file_id ORDER BY created_at) AS photos
              FROM poi_photo pp
              WHERE pp.poi_id = p.id
            ) ph ON TRUE
            WHERE ST_DWithin(p.geom, ST_MakePoint($1,$2)::geography, $3)
            GROUP BY p.id, p.title, p.category, p.description, p.geom, p.created_at, ph.photos
            HAVING COALESCE(MAX(pc.created_at), p.created_at) >= NOW() - INTERVAL '12 hours'
            ORDER BY p.geom <-> ST_MakePoint($1,$2)::geography
            LIMIT 50;
            """,
            float(lon), float(lat), float(radius_m)
        )

    if not rows:
        await cq.message.answer(L(lang, "no_points_in_radius", miles=miles))
        await bot.send_message(cq.message.chat.id, L(lang, "back_to_start"), reply_markup=home_kb())
        await cq.answer()
        return

    for r in rows[:15]:
        dist_mi = r["dist_m"] / 1609.344
        gmaps = f"https://www.google.com/maps/search/?api=1&query={r['lat']},{r['lon']}"
        has_conf = r["last_conf"] is not None
        ts = r["last_conf"] if has_conf else r["created_at"]
        when_label = ("последнее подтверждение" if has_conf else "добавлено")

        if r["description"]:
            header = f"📍 <b>{r['category']}</b> — {r['description']}\n"
        else:
            header = f"📍 <b>{r['category']}</b>\n"

        caption = (
            header
            + f"⏱ {when_label}: <b>{fmt_ts(ts)}</b>\n"
            + f"~{dist_mi:.1f} mi • Подтверждений: <b>{r['confirmations']}</b>\n"
            + f"<a href='{gmaps}'>Открыть в карте</a> • (Подтвердить точку)"
        )

        photos: Optional[List[str]] = r["photos"]
        if photos:
            medias: List[InputMediaPhoto] = []
            for i, fid in enumerate(photos[:10]):
                if i == 0:
                    medias.append(InputMediaPhoto(media=fid, caption=caption, parse_mode=ParseMode.HTML))
                else:
                    medias.append(InputMediaPhoto(media=fid))
            await bot.send_media_group(chat_id=cq.message.chat.id, media=medias)
            await bot.send_message(
                chat_id=cq.message.chat.id,
                text="Нажмите, чтобы подтвердить точку:",
                reply_markup=confirm_kb(r["id"], lang)
            )
        else:
            await cq.message.answer(caption, disable_web_page_preview=True, reply_markup=confirm_kb(r["id"], lang))

    await bot.send_message(cq.message.chat.id, L(lang, "back_to_start"), reply_markup=home_kb())
    await cq.answer()

# ===== Инструкция для Live-локации =====
@router.message(F.text.lower().contains("live"))
async def live_help(m: Message):
    lang = await get_user_lang(m.from_user.id)
    if not await has_active_subscription(m.from_user.id):
        await m.answer(L(lang, "need_active_sub"),
                       reply_markup=start_kb(lang, await has_active_subscription(m.from_user.id)))
        return
    await m.answer(L(lang, "live_howto"),
                   reply_markup=start_kb(lang, await has_active_subscription(m.from_user.id)))

# ===== Обработка Live-локации =====
@router.message(StateFilter(None), F.location & (F.location.live_period != None))
async def on_live_location_start(m: Message):
    lang = await get_user_lang(m.from_user.id)
    if not await has_active_subscription(m.from_user.id):
        await m.answer(L(lang, "need_active_sub"))
        return

    lat, lon = m.location.latitude, m.location.longitude
    last_loc[m.from_user.id] = (lat, lon, datetime.now(timezone.utc))

    assert pool is not None
    async with pool.acquire() as con:
        await con.execute(
            """
            UPDATE subscriptions
               SET center = ST_MakePoint($2,$3)::geography,
                   last_refreshed_at = NOW(),
                   is_active = TRUE
             WHERE tg_user_id = $1
            """,
            m.from_user.id, float(lon), float(lat)
        )

    live_period = int(m.location.live_period or 0)
    expire_at = (m.date or datetime.now(timezone.utc)).astimezone(timezone.utc) + timedelta(seconds=live_period)
    await schedule_live_reminder(m.from_user.id, m.message_id, expire_at)

    await m.answer("✅ Live-location is ON. We'll auto-extend your subscription while it's active.")

@router.edited_message(F.location)
async def on_live_location_update(m: Message):
    if not await has_active_subscription(m.from_user.id):
        return
    lat, lon = m.location.latitude, m.location.longitude
    last_loc[m.from_user.id] = (lat, lon, datetime.now(timezone.utc))

    assert pool is not None
    async with pool.acquire() as con:
        await con.execute(
            """
            UPDATE subscriptions
               SET center = ST_MakePoint($2,$3)::geography,
                   last_refreshed_at = NOW(),
                   is_active = TRUE
             WHERE tg_user_id = $1
            """,
            m.from_user.id, float(lon), float(lat)
        )

# ===== Добавление точек (пошагово с мультифото) =====
@router.message(F.text.lower() == "➕ добавить точку")
@router.message(F.text.lower() == "➕ add spot")
@router.message(F.text.lower() == "➕ agregar punto")
@router.message(F.text.contains("إضافة"))
@router.message(F.text.contains("पॉइंट"))
async def add_start(m: Message, state: FSMContext):
    lang = await get_user_lang(m.from_user.id)
    await state.set_state(AddPoi.waiting_location)
    await m.answer("Ок! Пришли геолокацию новой точки.", reply_markup=location_request_kb(lang))

@router.message(AddPoi.waiting_location, F.location)
async def add_loc(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    await state.set_state(AddPoi.waiting_category)
    lang = await get_user_lang(m.from_user.id)
    await m.answer(L(lang, "choose_category"), reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CATEGORIES[0]), KeyboardButton(text=CATEGORIES[1])]],
        resize_keyboard=True,
        one_time_keyboard=True
    ))

@router.message(AddPoi.waiting_category, F.text)
async def add_category(m: Message, state: FSMContext):
    category = (m.text or "").strip()
    if category not in CATEGORIES:
        lang = await get_user_lang(m.from_user.id)
        await m.answer(L(lang, "category_wrong"), reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=CATEGORIES[0]), KeyboardButton(text=CATEGORIES[1])]],
            resize_keyboard=True,
            one_time_keyboard=True
        ))
        return
    await state.update_data(category=category)
    await state.set_state(AddPoi.waiting_description)
    lang = await get_user_lang(m.from_user.id)
    await m.answer(L(lang, "short_desc"), reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=("Пропустить"))]],
        resize_keyboard=True,
        one_time_keyboard=True
    ))

@router.message(AddPoi.waiting_description, F.text)
async def add_desc(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    desc = None if (txt == "" or txt == "-" or txt.lower() in ("пропустить", "skip", "omitir", "تخطي", "स्किप")) else txt
    await state.update_data(description=desc, photos=[])
    await state.set_state(AddPoi.waiting_photo)
    lang = await get_user_lang(m.from_user.id)
    done_or_skip = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=("Готово")),],[KeyboardButton(text=("Пропустить"))]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await m.answer(L(lang, "send_photos"), reply_markup=done_or_skip)

@router.message(AddPoi.waiting_photo, F.photo)
async def add_collect_photo(m: Message, state: FSMContext):
    file_id = m.photo[-1].file_id
    data = await state.get_data()
    photos: List[str] = data.get("photos", [])
    photos.append(file_id)
    await state.update_data(photos=photos)
    lang = await get_user_lang(m.from_user.id)
    done_or_skip = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=("Готово")),],[KeyboardButton(text=("Пропустить"))]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await m.answer(L(lang, "photo_saved", n=len(photos)), reply_markup=done_or_skip)

@router.message(AddPoi.waiting_photo, F.text.lower() == "готово")
async def add_finish_with_photos(m: Message, state: FSMContext):
    data = await state.get_data()
    photos: List[str] = data.get("photos", [])
    await finalize_poi_creation(m, state, photos)

@router.message(AddPoi.waiting_photo, F.text.lower() == "пропустить")
async def add_skip_photos(m: Message, state: FSMContext):
    await finalize_poi_creation(m, state, [])

async def finalize_poi_creation(m: Message, state: FSMContext, file_ids: List[str]):
    data = await state.get_data()
    lat, lon = float(data["lat"]), float(data["lon"])
    category = data["category"]
    title = category
    desc = data.get("description")
    user_row_id = await ensure_user(m.from_user.id, m.from_user.username)

    # ---- ЛИМИТ: не более 2 точек за 12 часов ----
    assert pool is not None
    async with pool.acquire() as con:
        recent_cnt = await con.fetchval(
            """
            SELECT COUNT(*)
            FROM poi
            WHERE created_by = $1 AND created_at >= NOW() - INTERVAL '12 hours'
            """,
            user_row_id,
        )
        if int(recent_cnt or 0) >= ADD_LIMIT_COUNT:
            lang = await get_user_lang(m.from_user.id)
            await m.answer(L(lang, "limit_reached"),
                           reply_markup=start_kb(lang, await has_active_subscription(m.from_user.id)))
            return

        row = await con.fetchrow(
            """
            INSERT INTO poi(title, category, geom, description, created_by)
            VALUES ($1,$2, ST_MakePoint($3,$4)::geography, $5, $6)
            RETURNING id, created_at
            """,
            title, category, float(lon), float(lat), desc, user_row_id
        )
        poi_id = int(row["id"])
        for fid in file_ids[:20]:
            await con.execute(
                "INSERT INTO poi_photo(poi_id, file_id, user_id) VALUES ($1,$2,$3)",
                poi_id, fid, user_row_id
            )

    await state.clear()
    gmaps = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    photo_note = f" с фото ({len(file_ids)})" if file_ids else ""

    lang = await get_user_lang(m.from_user.id)
    header = L(lang, "point_added", photo=photo_note, category=category)
    if desc:
        header += f"{desc}\n"

    await m.answer(
        header +
        L(lang, "added_at", ts=fmt_ts(row['created_at'])) +
        f"<a href='{gmaps}'>Открыть в карте</a>",
        disable_web_page_preview=True,
        reply_markup=start_kb(lang, await has_active_subscription(m.from_user.id))
    )

    await notify_subscribers(
        poi_id=poi_id,
        lat=lat, lon=lon,
        category=category,
        description=desc,
        created_at=row['created_at'],
        photos=file_ids[:10],
        exclude_tg_id=m.from_user.id
    )

# ===== 📡 ВКЛ/ВЫКЛ отслеживание (вместо «Подписаться») =====
@router.message(F.text.lower().contains("📡"))
async def tracking_toggle(m: Message):
    lang = await get_user_lang(m.from_user.id)

    if await has_active_subscription(m.from_user.id):
        # выключаем трекинг
        assert pool is not None
        async with pool.acquire() as con:
            await con.execute("UPDATE subscriptions SET is_active=false WHERE tg_user_id=$1", m.from_user.id)
        await m.answer(L(lang, "tracking_off"), reply_markup=start_kb(lang, False))
        return

    # включаем трекинг — попросим радиус, но центр нужен из последней локации
    if m.from_user.id not in last_loc:
        await m.answer(L(lang, "send_location_first"), reply_markup=location_request_kb(lang))
        return

    await m.answer(L(lang, "pick_tracking_radius"), reply_markup=track_radius_kb())

# Выбор радиуса для отслеживания
@router.callback_query(F.data.startswith("track:"))
async def on_track_radius(cq: CallbackQuery):
    uid = cq.from_user.id
    lang = await get_user_lang(uid)

    if uid not in last_loc:
        await cq.message.answer(L(lang, "send_location_first"), reply_markup=location_request_kb(lang))
        await cq.answer(); return

    miles = int(cq.data.split(":")[1])
    r_m = miles_to_meters(miles)
    lat, lon, _ = last_loc[uid]

    assert pool is not None
    async with pool.acquire() as con:
        await con.execute("DELETE FROM subscriptions WHERE tg_user_id=$1", uid)
        await con.execute(
            """
            INSERT INTO subscriptions(tg_user_id, center, radius_m, is_active, last_refreshed_at)
            VALUES ($1, ST_MakePoint($2,$3)::geography, $4, TRUE, NOW())
            """,
            uid, float(lon), float(lat), float(r_m)
        )

    await cq.message.answer(f"{L(lang,'tracking_on')} • {miles} mi")
    try:
        await bot.send_message(uid, L(lang, "tracking_on"), reply_markup=start_kb(lang, True))
    except Exception:
        pass
    await cq.answer(); return

# ===== Подписки: обратная совместимость для старых callback 'sub:' (если где-то остались) =====
@router.callback_query(F.data.startswith("sub:"))
async def sub_pick_radius_compat(cq: CallbackQuery):
    # просто проксируем в новую логику track:
    miles = cq.data.split(":")[1]
    cq.data = f"track:{miles}"
    return await on_track_radius(cq)

@router.message(F.text.lower() == "/mysub")
async def my_sub(m: Message):
    assert pool is not None
    async with pool.acquire() as con:
        await con.execute(
            """
            UPDATE subscriptions
            SET is_active=false
            WHERE is_active
              AND last_refreshed_at < NOW() - INTERVAL '30 days'
        """
        )
        rows = await con.fetch(
            """
            SELECT id, ST_Y(center::geometry) lat, ST_X(center::geometry) lon,
                   radius_m, is_active, created_at, last_refreshed_at
            FROM subscriptions WHERE tg_user_id=$1
            ORDER BY created_at DESC
        """, m.from_user.id)
    if not rows:
        await m.answer("Подписок нет. Нажми «📡 Отслеживание».")
        return
    out = []
    for r in rows:
        status = 'вкл' if r['is_active'] else 'выкл'
        out.append(f"#{r['id']} • {r['radius_m']/1609.344:.0f} mi • {status} • обновлено: {fmt_ts(r['last_refreshed_at'])}")
    await m.answer("\n".join(out))

@router.message(F.text.lower() == "/unsubscribe")
async def unsubscribe(m: Message):
    assert pool is not None
    async with pool.acquire() as con:
        await con.execute("UPDATE subscriptions SET is_active=false WHERE tg_user_id=$1", m.from_user.id)
    lang = await get_user_lang(m.from_user.id)
    await m.answer(L(lang, "tracking_off"))

async def notify_subscribers(
    poi_id: int,
    lat: float, lon: float,
    category: str,
    description: Optional[str],
    created_at: datetime,
    photos: Optional[List[str]] = None,
    exclude_tg_id: Optional[int] = None
):
    assert pool is not None
    async with pool.acquire() as con:
        # лениво выключаем очень старые (30d) — чтобы не копились
        await con.execute(
            """
            UPDATE subscriptions
            SET is_active=false
            WHERE is_active
              AND last_refreshed_at < NOW() - INTERVAL '30 days'
        """
        )
        sub_rows = await con.fetch(
            """
            SELECT DISTINCT tg_user_id
            FROM subscriptions
            WHERE is_active
              AND last_refreshed_at >= NOW() - INTERVAL '30 days'
              AND ST_DWithin(center, ST_MakePoint($1,$2)::geography, radius_m)
            """,
            float(lon), float(lat)
        )
        if photos is None:
            photos = await con.fetchval(
                """
                SELECT array_agg(file_id ORDER BY created_at)
                FROM poi_photo WHERE poi_id=$1
                """,
                poi_id
            ) or []

    if not sub_rows:
        return

    gmaps = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    header = f"🔔 Новая точка рядом #{poi_id}\n<b>{category}</b>\n"
    if description:
        header += f"{description}\n"
    caption = (
        header +
        f"⏱ добавлено: <b>{fmt_ts(created_at)}</b>\n" +
        f"<a href='{gmaps}'>Открыть в карте</a> • (Подтвердить точку)"
    )

    for r in sub_rows:
        tgid = int(r["tg_user_id"])
        if exclude_tg_id and tgid == exclude_tg_id:
            continue
        try:
            if photos:
                medias: List[InputMediaPhoto] = []
                for i, fid in enumerate(photos[:10]):
                    if i == 0:
                        medias.append(InputMediaPhoto(media=fid, caption=caption, parse_mode=ParseMode.HTML))
                    else:
                        medias.append(InputMediaPhoto(media=fid))
                await bot.send_media_group(chat_id=tgid, media=medias)
                await bot.send_message(
                    chat_id=tgid,
                    text="Нажмите, чтобы подтвердить точку:",
                    reply_markup=confirm_kb(poi_id, await get_user_lang(tgid))
                )
            else:
                await bot.send_message(tgid, caption, disable_web_page_preview=True,
                                       reply_markup=confirm_kb(poi_id, await get_user_lang(tgid)))
        except Exception:
            continue

# ===== Подтверждение =====
@router.message(F.text.startswith("/confirm_"))
async def confirm_cmd(m: Message):
    try:
        poi_id = int(m.text.split("_", 1)[1])
    except Exception:
        await m.answer("Неверный формат команды. Пример: /confirm_123")
        return
    await _do_confirm(user_id=m.from_user.id, username=m.from_user.username, poi_id=poi_id, reply_to=m)

@router.callback_query(F.data.startswith("cf:"))
async def confirm_cb(cq: CallbackQuery):
    try:
        poi_id = int(cq.data.split(":")[1])
    except Exception:
        await cq.answer("Неверная кнопка.")
        return
    await cq.answer("⏳ Проверяю...")
    await _do_confirm(user_id=cq.from_user.id, username=cq.from_user.username, poi_id=poi_id, reply_to=cq.message)

async def _do_confirm(user_id: int, username: Optional[str], poi_id: int, reply_to: Message):
    lang = await get_user_lang(user_id)
    if not is_location_fresh(user_id):
        await reply_to.answer(
            L(lang, "need_fresh_loc", mins=FRESH_LOCATION_MINUTES),
            reply_markup=location_request_kb(lang)
        )
        return

    user_lat, user_lon, _ = last_loc[user_id]
    user_row_id = await ensure_user(user_id, username)

    assert pool is not None
    async with pool.acquire() as con:
        poi = await con.fetchrow(
            """
            SELECT p.id, p.category, p.description, p.created_by,
                   ST_Y(p.geom::geometry) AS lat,
                   ST_X(p.geom::geometry) AS lon,
                   ST_Distance(p.geom, ST_MakePoint($1,$2)::geography) AS dist_m,
                   COALESCE(
                       (SELECT MAX(pc.created_at) FROM poi_confirmation pc WHERE pc.poi_id = p.id),
                       p.created_at
                   ) AS last_ts
            FROM poi p
            WHERE p.id = $3
            """,
            float(user_lon), float(user_lat), poi_id
        )

    if not poi:
        await reply_to.answer("Точка не найдена.")
        return

    last_ts: datetime = poi["last_ts"]
    if last_ts is None or (datetime.now(timezone.utc) - last_ts).total_seconds() > 12 * 3600:
        await reply_to.answer("Эта точка устарела (старше 12 часов).")
        return

    if poi["created_by"] == user_row_id:
        await reply_to.answer("Вы не можете подтверждать свою точку. Пусть подтвердят другие пользователи.")
        return

    max_m = miles_to_meters(CONFIRM_RADIUS_MI)
    if float(poi["dist_m"]) > max_m:
        await reply_to.answer(L(lang, "too_far_to_confirm"))
        return

    assert pool is not None
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO poi_confirmation(poi_id, user_id) VALUES ($1,$2) ON CONFLICT (poi_id, user_id) DO NOTHING",
            poi_id, user_row_id
        )
        cnt = await con.fetchval("SELECT COUNT(*) FROM poi_confirmation WHERE poi_id=$1", poi_id)

    await reply_to.answer(
        f"✅ Подтверждено: <b>{poi['category']}</b>\n" +
        f"Всего подтверждений: <b>{cnt}</b>"
    )

# ===== Фолбэк =====
@router.message(F.text)
async def fallback(m: Message):
    lang = await get_user_lang(m.from_user.id)
    await m.answer(
        "📍 " + L(lang, "send_location_first"),
        reply_markup=start_kb(lang, await has_active_subscription(m.from_user.id))
    )

# ========= MAIN =========
def main():
    dp.run_polling(bot)

if __name__ == "__main__":
    main()
