# main.py
import json
import warnings
import re
import sqlite3
import logging
import os

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError
from thefuzz import fuzz

# İKİ DOSYALI YAPI: Ayarlar config.py dosyasından import ediliyor.
from config import GROUP_CHAT_IDS, CITY_EMOJIS

# Loglama ayarları
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Gürültücü kütüphanelerin log seviyesini sadece önemli durumlar için ayarla
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=UserWarning)

# Aşamalar
POSITION, CITY, DISTRICT, DESCRIPTION, ASK_PHOTO, PHOTO, PREVIEW, EDIT_CHOICE, EDIT_RECEIVE_VALUE, \
ASK_CONTACT_METHOD, GET_CONTACT_INFO = range(11)

# --- Bot Ayarları ve Sabitler ---
BOT_TOKEN = "7564754548:AAGWiif1vGjbkqlGaaKrIxuHgmNnnwynUx8" # Lütfen kendi token'ınız ile değiştirin
ADMIN_ID = 5286354568
CHANNEL_CHAT_ID = -1002421814809

# Callback verileri için JSON formatını destekleyen sabitler
CB_GO_BACK = "back"
CB_CANCEL = "cancel"
CB_APPROVE = "approve"
CB_REJECT = "reject"


# --- Callback Veri Oluşturma Fonksiyonları ---
def create_callback_data(action: str, **kwargs) -> str:
    """JSON formatında güvenli callback datası oluşturur."""
    data = {"action": action}
    data.update(kwargs)
    return json.dumps(data, separators=(',', ':'))


# --- Veritabanı Fonksiyonları ---
def add_pending_post_to_db(user_id: int, post_data: dict, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = context.bot_data['db_connection']
        cursor = context.bot_data['db_cursor']
        post_data_json = json.dumps(post_data)
        cursor.execute("INSERT OR REPLACE INTO pending_posts (user_id, post_data) VALUES (?, ?)", (user_id, post_data_json))
        conn.commit()
    except Exception as e:
        logger.error(f"Veritabanına ilan eklenirken hata: {e}")


def get_pending_post_from_db(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        cursor = context.bot_data['db_cursor']
        cursor.execute("SELECT post_data FROM pending_posts WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return json.loads(result[0]) if result else None
    except Exception as e:
        logger.error(f"Veritabanından ilan alınırken hata: {e}")
        return None


def delete_pending_post_from_db(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = context.bot_data['db_connection']
        cursor = context.bot_data['db_cursor']
        cursor.execute("DELETE FROM pending_posts WHERE user_id = ?", (user_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"Veritabanından ilan silinirken hata: {e}")


def load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"'{path}' dosyası bulunamadı.")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"'{path}' dosyası geçerli bir JSON formatında değil. Detay: {e}")
        return {}


# --- Metin İşleme Fonksiyonları ---
TURKISH_LOWER_MAP = str.maketrans("ıöüçşğ", "ioucsg")


def normalize_text(text: str) -> str:
    """Türkçe karakterleri ve büyük harfleri normalize eder."""
    return text.lower().translate(TURKISH_LOWER_MAP)


def clean_key_for_matching(text: str) -> str:
    normalized = normalize_text(text)
    return re.sub(r'[^a-z0-9]', '', normalized)


def normalize_city_name_for_lookup(city_display_name: str) -> str:
    normalized = normalize_text(city_display_name)
    if "freelance" in normalized or "homeoffice" in normalized:
        return "Freelance / Home Office"
    return normalized

# ==========================================================
# SON DÜZELTME: Eşleştirme Fonksiyonunun Nihai Hali
# ==========================================================
def match_position_from_text(position_text, city, thread_map, keywords_config, description_text=""):
    logger.info(f"Yeni ilan eşleştiriliyor (Esnek Eşleştirme) -> Pozisyon: '{position_text}', Şehir: '{city}'")

    keywords_map = keywords_config.get("keyword_mappings", {})
    if not keywords_map: 
        logger.warning("keywords_config içinde 'keyword_mappings' bulunamadı.")
        return [], []

    norm_pos_text = normalize_text(position_text)
    norm_full_text = normalize_text(f"{position_text} {description_text}")
    topic_scores = {}

    SIMILARITY_THRESHOLD = 90

    for topic, data in keywords_map.items():
        current_score = 0
        title_matched = False

        if any(normalize_text(neg_kw) in norm_full_text for neg_kw in data.get("negative_keywords", [])):
            continue

        # DÜZELTME: Önce pozisyonun tamamını anahtar kelimelerle karşılaştır
        for title_kw in data.get("title_keywords", []):
            normalized_title_kw = normalize_text(title_kw)
            if fuzz.ratio(norm_pos_text, normalized_title_kw) >= SIMILARITY_THRESHOLD:
                current_score += 120
                title_matched = True
                break
        if title_matched:
            topic_scores[topic] = current_score
            # Tam eşleşme bulundu, bu kategori için diğer kontrollere gerek yok, skoru kaydet ve devam et
            # Ama diğer kategorileri de kontrol etmeye devam etmeli, bu yüzden continue yok.

        # DÜZELTME: Eğer tam eşleşme yoksa veya ek puan için, kelime bazlı karşılaştırmaya devam et
        # Not: Yukarıda eşleşme olsa bile, başka bir kelime daha eşleşirse skoru artırabilir.
        for title_kw in data.get("title_keywords", []):
            normalized_title_kw = normalize_text(title_kw)
            for word_in_title in norm_pos_text.split():
                # Eğer daha önce tam eşleşme olduysa, aynı anahtar kelimeyi tekrar sayma
                if title_matched and norm_pos_text == normalized_title_kw: continue
                if fuzz.ratio(word_in_title, normalized_title_kw) >= SIMILARITY_THRESHOLD:
                    current_score += 120
                    title_matched = True # Kelime bazlı da olsa eşleşme var
                    break
            if title_matched and not (norm_pos_text == normalized_title_kw) : # if a word matched, no need to check other title keywords
                break

        # Kelime bazlı skoru ekle
        if current_score > 0 and topic not in topic_scores:
             topic_scores[topic] = current_score

        # Bağlam kelimelerini her zaman kontrol et ve skora ekle
        context_score = 0
        for context_kw in data.get("context_keywords", []):
            if re.search(r'\b' + re.escape(normalize_text(context_kw)) + r'\b', norm_full_text):
                context_score += 30

        if context_score > 0:
            topic_scores[topic] = topic_scores.get(topic, 0) + context_score

    if not topic_scores:
        logger.info("Yeterli puana ulaşan kategori bulunamadı.")
        return [], []

    score_threshold = 50
    high_scoring_topics = {topic: score for topic, score in topic_scores.items() if score >= score_threshold}

    if not high_scoring_topics:
        logger.info(f"Puanlar barajı geçemedi: {topic_scores}")
        return [], []

    sorted_topics = sorted(high_scoring_topics.items(), key=lambda item: item[1], reverse=True)

    logger.info(f"Puanı geçen konular (sıralı): {sorted_topics}")

    city_key_lookup = normalize_city_name_for_lookup(city)
    cleaned_city_key_lookup = clean_key_for_matching(city_key_lookup)

    city_threads = {}
    for key, value in thread_map.items():
        if clean_key_for_matching(key) == cleaned_city_key_lookup:
            city_threads = value
            logger.info(f"Şehir eşleşmesi başarılı: '{city}' şehri için '{key}' anahtarı bulundu.")
            break

    if not city_threads:
        logger.warning(f"Şehir eşleşmesi BAŞARISIZ: '{city}' (aranan: '{cleaned_city_key_lookup}') thread_map.json'da bulunamadı.")
        return [], []

    cleaned_city_threads = {clean_key_for_matching(k): (v, k) for k, v in city_threads.items()}

    matched_thread_ids, matched_position_names = [], []
    for topic, score in sorted_topics:
        if len(matched_thread_ids) >= 3: break

        cleaned_topic_key = clean_key_for_matching(topic)
        if cleaned_topic_key in cleaned_city_threads:
            thread_id, original_key = cleaned_city_threads[cleaned_topic_key]
            if thread_id not in matched_thread_ids:
                matched_thread_ids.append(thread_id)
                matched_position_names.append(original_key)
                logger.info(f"✅ BAŞARILI KATEGORİ EŞLEŞMESİ: '{topic}' -> '{original_key}' (ID: {thread_id})")
        else:
            logger.warning(
                f"⚠️ UYARI: '{topic}' kategorisi eşleşti ANCAK '{city}' için thread haritasında bulunamadı. (Aranan anahtar: '{cleaned_topic_key}')")

    return matched_position_names, matched_thread_ids

# --- Conversation Handlers ---
def create_navigation_keyboard(back_state: int = None):
    row = []
    if back_state is not None:
        row.append(InlineKeyboardButton("⬅️ Geri", callback_data=create_callback_data(CB_GO_BACK, state=back_state)))
    row.append(InlineKeyboardButton("❌ İptal", callback_data=create_callback_data(CB_CANCEL)))
    return InlineKeyboardMarkup([row])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.chat.type != "private": return
    if context.user_data.get('in_conversation'):
        await update.message.reply_text("Zaten bir ilan oluşturma sürecindesiniz. Lütfen `/cancel` ile iptal edin.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['in_conversation'] = True
    context.user_data['user_mention_html'] = update.effective_user.mention_html()
    welcome_text = "🎉 <b>İşegel Asistan'a Hoş Geldin!</b> 🎉\n\nHaydi başlayalım! Lütfen ilan vermek istediğiniz <b>Pozisyonu</b> yazınız:"

    await update.message.reply_text(
        welcome_text,
        parse_mode="HTML",
        reply_markup=create_navigation_keyboard()
    )
    return POSITION


async def position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message if isinstance(update, Update) else update
    if isinstance(update, Update):
        context.user_data["position"] = message.text

    buttons = []
    ordered_cities = ["istanbul", "ankara", "izmir", "kocaeli", "bursa", "manisa", "adana", "konya", "antalya",
                      "samsun", "kayseri", "gaziantep"]
    row = []
    for city_key in ordered_cities:
        if city_key in GROUP_CHAT_IDS:
            emoji = CITY_EMOJIS.get(city_key, "🏙️")
            display_name = city_key.title()
            row.append(InlineKeyboardButton(f"{emoji} {display_name}", callback_data=city_key))
            if len(row) == 3:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(f"{CITY_EMOJIS.get('Freelance / Home Office', '💻')} Freelance / Home Office",
                                           callback_data="Freelance / Home Office")])
    buttons.append([InlineKeyboardButton("➕ Diğer", callback_data="Diğer")])
    buttons.append([
        InlineKeyboardButton("⬅️ Geri", callback_data=create_callback_data(CB_GO_BACK, state=POSITION)),
        InlineKeyboardButton("❌ İptal", callback_data=create_callback_data(CB_CANCEL))
    ])

    await context.bot.send_message(
        chat_id=message.chat_id,
        text="📍 Harika, şimdi lütfen ilanın yayınlanacağı şehri seçin:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    return CITY


async def city_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["city"] = query.data

    await query.message.reply_text(
        "🗺️ Lütfen <b>ilçeyi</b> yazınız:",
        parse_mode="HTML",
        reply_markup=create_navigation_keyboard(back_state=CITY)
    )
    return DISTRICT


async def district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message if isinstance(update, Update) else update
    if isinstance(update, Update):
        context.user_data["district"] = message.text

    buttons = [
        [InlineKeyboardButton("📎 Evet", callback_data="photo_yes"),
         InlineKeyboardButton("🚫 Hayır", callback_data="photo_no")],
        [
            InlineKeyboardButton("⬅️ Geri", callback_data=create_callback_data(CB_GO_BACK, state=DISTRICT)),
            InlineKeyboardButton("❌ İptal", callback_data=create_callback_data(CB_CANCEL))
        ]
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    await context.bot.send_message(
        chat_id=message.chat_id,
        text="🖼️ İlanınıza görsel eklemek ister misiniz?",
        reply_markup=keyboard
    )
    return ASK_PHOTO


async def ask_photo_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "photo_yes":
        await query.edit_message_text(
            "📷 Lütfen görseli gönderin:",
            reply_markup=create_navigation_keyboard(back_state=ASK_PHOTO)
        )
        return PHOTO
    context.user_data["photo"] = None
    await query.edit_message_text(
        "📝 Şimdi de iş tanımını ve aranan nitelikleri içeren ilan açıklamasını yazınız:",
        reply_markup=create_navigation_keyboard(back_state=ASK_PHOTO)
    )
    return DESCRIPTION


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["photo"] = update.message.photo[-1].file_id
    await update.message.reply_text(
        "📝 Harika! Şimdi de iş tanımını ve aranan nitelikleri içeren ilan açıklamasını yazınız:",
        reply_markup=create_navigation_keyboard(back_state=PHOTO)
    )
    return DESCRIPTION


async def description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["description"] = update.message.text
    keyboard_buttons = [
        [InlineKeyboardButton("📞 Telefon", callback_data="contact_phone"),
         InlineKeyboardButton("✉️ E-posta", callback_data="contact_email")],
        [InlineKeyboardButton("🔗 Başvuru Linki", callback_data="contact_link"),
         InlineKeyboardButton("👤 Telegram", callback_data="contact_telegram")],
        [
            InlineKeyboardButton("⬅️ Geri", callback_data=create_callback_data(CB_GO_BACK, state=DESCRIPTION)),
            InlineKeyboardButton("❌ İptal", callback_data=create_callback_data(CB_CANCEL))
        ]
    ]
    await update.message.reply_text("📋 Çok iyi! Son olarak, başvurular nasıl alınsın?",
                                      reply_markup=InlineKeyboardMarkup(keyboard_buttons))
    return ASK_CONTACT_METHOD


async def prompt_for_contact_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data
    context.user_data['contact_method'] = choice.split('_')[1]

    if choice == "contact_telegram":
        context.user_data['contact_info'] = context.user_data.get('user_mention_html', 'Telegram Üzerinden')
        await query.edit_message_text("✅ Anlaşıldı. İlanınızın önizlemesi hazırlanıyor...")
        return await preview_post(query.message, context)

    prompts = {
        "contact_phone": "Lütfen <b>telefon numarasını</b> yazınız (Örn: 0555 123 4567):",
        "contact_email": "Lütfen <b>e-posta adresini</b> yazınız:",
        "contact_link": "Lütfen <b>başvuru linkini (URL)</b> yapıştırınız:"
    }

    await query.edit_message_text(
        prompts[choice],
        parse_mode="HTML",
        reply_markup=create_navigation_keyboard(back_state=ASK_CONTACT_METHOD)
    )
    return GET_CONTACT_INFO


async def get_contact_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcının girdiği iletişim bilgisini alır ve doğrular."""
    contact_method = context.user_data.get('contact_method')
    user_input = update.message.text.strip()

    # E-posta doğrulaması
    if contact_method == 'email' and not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", user_input):
        await update.message.reply_text("❌ Geçersiz e-posta formatı. Lütfen doğru bir e-posta adresi yazın:")
        return GET_CONTACT_INFO

    # Telefon numarası doğrulaması (en az 10 rakam olmalı)
    if contact_method == 'phone':
        cleaned_phone = re.sub(r'\D', '', user_input)
        if len(cleaned_phone) < 10:
            await update.message.reply_text("❌ Geçersiz telefon numarası. Lütfen numaranızı kontrol edip tekrar yazın:")
            return GET_CONTACT_INFO

    # Link doğrulaması (http/https ekle)
    if contact_method == 'link' and not (user_input.lower().startswith('http://') or user_input.lower().startswith('https://')):
        user_input = f"https://{user_input}"

    context.user_data['contact_info'] = user_input
    return await preview_post(update.message, context)


def create_post_text(context: ContextTypes.DEFAULT_TYPE):
    """İlan metnini oluşturan ve başvuru bilgilerini tıklanabilir linklere çeviren fonksiyon."""
    data = context.user_data
    city_name = data.get('city', 'N/A')
    display_city_name = city_name.title()
    contact_method = data.get('contact_method')
    contact_info = data.get('contact_info', '').strip()
    contact_line = ""

    if contact_method == 'phone':
        cleaned_phone = re.sub(r'\D', '', contact_info)
        contact_line = f"📞 <b>Başvuru:</b> <a href=\"tel:{cleaned_phone}\">{contact_info}</a>" if cleaned_phone else f"📞 <b>Başvuru:</b> {contact_info}"
    elif contact_method == 'email':
        contact_line = f"✉️ <b>Başvuru:</b> <a href=\"mailto:{contact_info}\">{contact_info}</a>"
    elif contact_method == 'link':
        href = contact_info
        if not href.lower().startswith(('http://', 'https://')):
            href = f"https://{href}"
        contact_line = f"🔗 <b>Başvuru:</b> <a href=\"{href}\">{contact_info}</a>"
    elif contact_method == 'telegram':
        contact_line = f"👤 <b>Başvuru:</b> {data.get('user_mention_html', 'Telegram Üzerinden')}"

    base_hashtags = "#kariyer #işealım #işilanları #personel #eleman #işbaşvurusu #insankaynakları #ik"
    city_mention = ""
    if city_name and city_name not in ["Diğer", "Freelance / Home Office"]:
        slug = normalize_text(city_name).replace(" ", "")
        city_mention = f"@isegel_{slug}"
    tags_and_mentions = f"{base_hashtags} {city_mention}".strip()

    return (f"📢 <b>Yeni İş İlanı  - isegel.net</b>\n\n"
            f"📌 <b>Pozisyon:</b> {data.get('position', 'N/A')}\n"
            f"📍 <b>Lokasyon:</b> {display_city_name} / {data.get('district', 'N/A')}\n\n"
            f"📝 <b>Açıklama:</b>\n{data.get('description', 'N/A')}\n\n"
            f"{contact_line}\n\n"
            "────────────────────\n"
            f"{tags_and_mentions}\n\n"
            "📣 Siz de ilan vermek için @isegel_bot botunu kullanabilirsiniz.")


async def preview_post(message, context: ContextTypes.DEFAULT_TYPE):
    for key in ['editing', 'field_to_edit']:
        if key in context.user_data: del context.user_data[key]
    preview_text = create_post_text(context)
    photo_file_id = context.user_data.get("photo")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Onayla ve Gönder", callback_data="user_confirm")],
        [InlineKeyboardButton("✏️ Alan Düzenle", callback_data="user_edit_menu")],
        [InlineKeyboardButton("❌ İptal Et", callback_data="user_cancel")]])
    if photo_file_id:
        await message.reply_photo(photo=photo_file_id, caption=preview_text, parse_mode="HTML",
                                  reply_markup=keyboard)
    else:
        await message.reply_text(text=preview_text, parse_mode="HTML", reply_markup=keyboard)
    return PREVIEW


async def handle_user_preview_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcının önizleme ekranındaki seçimini (Onayla, Düzenle, İptal) yönetir."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    choice = query.data

    if choice == "user_confirm":
        context.user_data['in_conversation'] = False
        caption_text = (query.message.caption or query.message.text) + "\n\n⌛ <i>İlanınız yönetici onayına gönderiliyor...</i>"

        if query.message.caption:
            await query.edit_message_caption(caption=caption_text, parse_mode="HTML", reply_markup=None)
        else:
            await query.edit_message_text(text=caption_text, parse_mode="HTML", reply_markup=None)

        return await finalize_post_to_admin(query.message, context, user_id)

    elif choice == "user_cancel":
        await query.edit_message_text("❌ İşlem iptal edildi.")
        context.user_data.clear()
        return ConversationHandler.END


async def show_edit_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcıya düzenleyebileceği alanların butonlarını gösterir."""
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Pozisyon", callback_data="edit_field_position")],
        [InlineKeyboardButton("İlçe", callback_data="edit_field_district")],
        [InlineKeyboardButton("Açıklama", callback_data="edit_field_description")],
        [InlineKeyboardButton("Başvuru Yöntemi", callback_data="edit_field_contact")],
        [InlineKeyboardButton("⬅️ Geri", callback_data="edit_back_to_preview")]])
    await query.edit_message_text("Hangi alanı düzenlemek istersiniz?", reply_markup=keyboard)
    return EDIT_CHOICE


async def request_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Düzenlenecek alan için kullanıcıdan yeni bir değer girmesini ister."""
    query = update.callback_query
    await query.answer()
    field_to_edit = query.data.split('_')[-1]

    if field_to_edit == 'contact':
        keyboard = [
            [InlineKeyboardButton("📞 Telefon", callback_data="contact_phone"),
             InlineKeyboardButton("✉️ E-posta", callback_data="contact_email")],
            [InlineKeyboardButton("🔗 Başvuru Linki", callback_data="contact_link"),
             InlineKeyboardButton("👤 Telegram", callback_data="contact_telegram")]]
        await query.edit_message_text("Yeni başvuru yöntemi ne olmalı?", reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_CONTACT_METHOD

    context.user_data.update({'field_to_edit': field_to_edit, 'editing': True})
    field_map = {"position": "pozisyonu", "district": "ilçeyi", "description": "açıklamayı"}
    await query.edit_message_text(f"Lütfen yeni <b>{field_map.get(field_to_edit, 'değeri')}</b> yazınız:",
                                  parse_mode="HTML")
    return EDIT_RECEIVE_VALUE


async def receive_edited_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcının girdiği yeni değeri alıp günceller ve önizlemeye döner."""
    field = context.user_data.get('field_to_edit')
    if not field: return PREVIEW
    context.user_data[field] = update.message.text
    await update.message.reply_text("✅ Alan güncellendi. İşte yeni önizleme:")
    return await preview_post(update.message, context)


async def back_to_preview_from_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Düzenleme menüsünden önizlemeye geri döner."""
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    return await preview_post(query.message, context)


# --- Yönetici Onay ve Gönderim Akışı ---
async def finalize_post_to_admin(message, context, user_id):
    post_text = create_post_text(context)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Onayla", callback_data=create_callback_data(CB_APPROVE, uid=user_id))],
        [InlineKeyboardButton("❌ Reddet", callback_data=create_callback_data(CB_REJECT, uid=user_id))]
    ])
    try:
        photo = context.user_data.get("photo")

        if photo:
            msg = await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo,
                caption=post_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            msg = await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=post_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )

        post_to_save = context.user_data.copy()
        post_to_save.update({"text": post_text, "message_id": msg.message_id, "user_chat_id": message.chat.id})
        add_pending_post_to_db(user_id, post_to_save, context)
        await context.bot.send_message(chat_id=user_id, text="İlanınız başarıyla yönetici onayına gönderildi. ✨")
    except TelegramError as e:
        logger.error(f"Yöneticiye ilan gönderilirken hata: {e}")
        await context.bot.send_message(chat_id=user_id, text="Bir hata oluştu, ilan gönderilemedi.")
    return ConversationHandler.END


async def _send_post(context: ContextTypes.DEFAULT_TYPE, chat_id: int, post_data: dict, thread_id: int = None, reply_markup=None):
    try:
        text, photo = post_data["text"], post_data.get("photo")
        if photo:
            await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=text, parse_mode="HTML",
                                         message_thread_id=thread_id, disable_notification=True, reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                           message_thread_id=thread_id, disable_notification=True, reply_markup=reply_markup)
        logger.info(f"Sessiz ilan gönderildi. Chat ID: {chat_id}, Thread ID: {thread_id}")
    except TelegramError as e:
        logger.error(f"Post gönderilemedi (ChatID: {chat_id}, ThreadID: {thread_id}): {e}")
        await context.bot.send_message(chat_id=ADMIN_ID,
                                       text=f"⚠️ <b>Gönderim Hatası</b>\nChatID: {chat_id}\nThreadID: {thread_id}\nHata: {e}",
                                       parse_mode="HTML")


async def _send_listing_to_channels(context: ContextTypes.DEFAULT_TYPE, post_data: dict):
    """İlanı ilgili kanallara, gruplara ve konulara gönderir."""
    thread_map = context.bot_data.get("thread_map", {})
    keywords_config = context.bot_data.get("keywords_config", {})

    matched_position_names, matched_thread_ids = match_position_from_text(
        post_data["position"], post_data["city"], thread_map, keywords_config, post_data["description"]
    )

    city_key_lookup = normalize_city_name_for_lookup(post_data["city"])
    cleaned_city_key = clean_key_for_matching(city_key_lookup)
    chat_id_group = GROUP_CHAT_IDS.get(cleaned_city_key) or GROUP_CHAT_IDS.get(post_data["city"].lower())

    group_keyboard = None
    if chat_id_group:
        url_slug = cleaned_city_key.replace('freelancehomeoffice', 'freelance')
        grup_link = f"https://t.me/isegel_{url_slug}"
        button_text = f"💬 {post_data['city'].title()} İş İlanları Grubu"
        group_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=grup_link)]])

    await _send_post(context, CHANNEL_CHAT_ID, post_data, reply_markup=group_keyboard)

    if chat_id_group and post_data["city"] != "Diğer":
        await _send_post(context, chat_id_group, post_data, reply_markup=group_keyboard)
        for thread_id in matched_thread_ids:
            if thread_id != 0:
                await _send_post(context, chat_id_group, post_data, thread_id=thread_id)

    return matched_position_names, chat_id_group


async def _notify_user_on_success(context: ContextTypes.DEFAULT_TYPE, post_data: dict, chat_id_group: int):
    """Kullanıcıyı ilanın yayınlandığına dair bilgilendirir."""
    grup_link = ""
    if chat_id_group:
        city_key_lookup = normalize_city_name_for_lookup(post_data["city"])
        cleaned_city_key = clean_key_for_matching(city_key_lookup)
        url_slug = cleaned_city_key.replace('freelancehomeoffice', 'freelance')
        grup_link = f"https://t.me/isegel_{url_slug}"

    mesaj = (f"✅ <b>İLANINIZ YAYINLANDI</b>\n"
             f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
             f"📢 Kanalda görüntülemek için:\nhttps://t.me/isegel_net")
    if grup_link:
        mesaj += f"\n\n💬 Grupta görüntülemek için:\n{grup_link}"

    await context.bot.send_message(
        chat_id=post_data["user_chat_id"],
        text=mesaj,
        parse_mode="HTML",
        disable_web_page_preview=False
    )

async def _update_admin_message_status(query: Update.callback_query, status_html: str):
    """Yöneticiye gönderilen ilanın metnini durum bilgisiyle günceller."""
    base_text = query.message.caption or query.message.text
    new_text = f"{base_text}\n\n{status_html}"
    if query.message.caption:
        await query.edit_message_caption(caption=new_text, parse_mode="HTML")
    else:
        await query.edit_message_text(text=new_text, parse_mode="HTML")


async def _process_approval(query: Update.callback_query, context: ContextTypes.DEFAULT_TYPE, user_id: int, post_data: dict):
    """Onaylama işleminin tüm adımlarını yönetir."""
    try:
        await _update_admin_message_status(query, "✅ <b>İLAN ONAYLANDI...</b>")

        matched_names, group_id = await _send_listing_to_channels(context, post_data)
        await _notify_user_on_success(context, post_data, group_id)

        admin_matched_text = ", ".join(matched_names) if matched_names else "Eşleşme Yok"
        display_city_name = post_data['city'].title()
        await context.bot.send_message(chat_id=ADMIN_ID,
                                       text=f"✅ <b>{display_city_name} / {admin_matched_text}</b> ilanı yayınlandı.",
                                       parse_mode="HTML")
    except Exception as e:
        logger.critical(f"ONAY İŞLEMİNDE KRİTİK HATA: {e}", exc_info=True)
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Kritik Onay Hatası: {e}")


async def _process_rejection(query: Update.callback_query, context: ContextTypes.DEFAULT_TYPE, post_data: dict):
    """Reddetme işlemini ve bildirimleri yönetir."""
    try:
        await _update_admin_message_status(query, "❌ <b>İLAN REDDEDİLDİ.</b>")
        await context.bot.send_message(chat_id=post_data["user_chat_id"], text="❌ Üzgünüz, ilanınız reddedildi.")
    except Exception as e:
        logger.error(f"Reddetme işlemi sırasında hata: {e}")


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yönetici butonlarını (onay/ret) işleyen ana yönlendirici."""
    query = update.callback_query
    await query.answer("✅ İşlem yapılıyor...")

    callback_data = json.loads(query.data)
    action = callback_data["action"]
    user_id = callback_data["uid"]

    post_data = get_pending_post_from_db(user_id, context)
    if not post_data:
        await query.edit_message_text("⚠️ Bu ilan zaten işlenmiş veya bulunamadı.")
        return

    if action == CB_APPROVE:
        await _process_approval(query, context, user_id, post_data)
    elif action == CB_REJECT:
        await _process_rejection(query, context, post_data)

    delete_pending_post_from_db(user_id, context)


# --- Navigasyon Handlerları (İptal, Geri) ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ İşlem iptal edildi.")
    context.user_data.clear()
    return ConversationHandler.END


async def handle_navigation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Geri ve İptal butonlarını JSON callback datasına göre yönetir."""
    query = update.callback_query
    await query.answer()

    callback_data = json.loads(query.data)
    action = callback_data["action"]

    if action == CB_CANCEL:
        await query.edit_message_text("❌ İşlem isteğiniz üzerine iptal edildi.")
        context.user_data.clear()
        return ConversationHandler.END

    elif action == CB_GO_BACK:
        previous_state = callback_data["state"]
        state_replay_map = {
            POSITION: _replay_position_step,
            CITY: _replay_city_step,
            DISTRICT: _replay_district_step,
            ASK_PHOTO: _replay_ask_photo_step,
            PHOTO: _replay_description_step,
            DESCRIPTION: _replay_description_step,
            ASK_CONTACT_METHOD: _replay_ask_contact_method
        }
        replay_function = state_replay_map.get(previous_state)
        if replay_function:
            return await replay_function(query, context)
    return ConversationHandler.END


async def _replay_position_step(query, context):
    context.user_data.pop('position', None)
    await query.edit_message_text("Lütfen ilan vermek istediğiniz <b>Pozisyonu</b> yeniden yazınız:",
                                  parse_mode="HTML", reply_markup=create_navigation_keyboard())
    return POSITION


async def _replay_city_step(query, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('city', None)
    await query.message.delete()
    await position(query.message, context)
    return CITY


async def _replay_district_step(query, context):
    context.user_data.pop('district', None)
    await query.edit_message_text("🗺️ Lütfen <b>ilçeyi</b> yeniden yazınız:",
                                  parse_mode="HTML", reply_markup=create_navigation_keyboard(back_state=CITY))
    return DISTRICT


async def _replay_ask_photo_step(query, context):
    context.user_data.pop('photo', None)
    await query.message.delete()
    await district(query.message, context)
    return ASK_PHOTO


async def _replay_description_step(query, context):
    context.user_data.pop('description', None)
    await query.edit_message_text(
        "📝 Şimdi de iş tanımını ve aranan nitelikleri içeren ilan açıklamasını yeniden yazınız:",
        reply_markup=create_navigation_keyboard(back_state=ASK_PHOTO))
    return DESCRIPTION


async def _replay_ask_contact_method(query, context):
    context.user_data.pop('contact_method', None)
    context.user_data.pop('contact_info', None)
    await query.message.delete()
    await description(query.message, context)
    return ASK_CONTACT_METHOD


# --- Uygulama Kurulumu ---
async def post_init(application: Application):
    # Veritabanı kurulumu
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    application.bot_data['db_connection'] = conn
    application.bot_data['db_cursor'] = cursor
    cursor.execute("CREATE TABLE IF NOT EXISTS pending_posts (user_id INTEGER PRIMARY KEY, post_data TEXT)")
    conn.commit()
    logger.info("Veritabanı bağlantısı kuruldu ve tablo hazır.")

    # Yapılandırma dosyaları bot belleğine yükleniyor
    application.bot_data['thread_map'] = load_json_file("thread_map.json")
    # Cache temizleme yöntemi için yeni isimli dosyayı okuyoruz
    application.bot_data['keywords_config'] = load_json_file("config_keywords_v2.json")
    logger.info("Yapılandırma dosyaları (thread_map, keywords_config) belleğe yüklendi.")


async def post_shutdown(application: Application):
    conn = application.bot_data.get('db_connection')
    if conn:
        conn.close()
        logger.info("Veritabanı bağlantısı güvenli bir şekilde kapatıldı.")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    navigation_handler = CallbackQueryHandler(handle_navigation_callback,
                                              pattern=f'^{{"action":"({CB_GO_BACK}|{CB_CANCEL})"')

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, start)
        ],
        states={
            POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, position)],
            CITY: [CallbackQueryHandler(city_selected, pattern=r"^(?!{.*}).*$")],
            DISTRICT: [MessageHandler(filters.TEXT & ~filters.COMMAND, district)],
            ASK_PHOTO: [CallbackQueryHandler(ask_photo_choice, pattern="^photo_")],
            PHOTO: [MessageHandler(filters.PHOTO, photo_handler)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description)],
            ASK_CONTACT_METHOD: [CallbackQueryHandler(prompt_for_contact_detail, pattern="^contact_")],
            GET_CONTACT_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contact_info)],
            PREVIEW: [
                CallbackQueryHandler(handle_user_preview_choice, pattern="^(user_confirm|user_cancel)$"),
                CallbackQueryHandler(show_edit_options, pattern="^user_edit_menu$")
            ],
            EDIT_CHOICE: [
                CallbackQueryHandler(request_new_value, pattern="^edit_field_"),
                CallbackQueryHandler(back_to_preview_from_edit, pattern="^edit_back_to_preview$")
            ],
            EDIT_RECEIVE_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edited_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    for state_handlers in conv_handler.states.values():
        state_handlers.append(navigation_handler)

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=f'^{{"action":"({CB_APPROVE}|{CB_REJECT})"'))

    logger.info("🤖 Bot çalışıyor...")
    app.run_polling()


if __name__ == "__main__":
    main()