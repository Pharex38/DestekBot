"""
Telegram Business destek asistani.
Kisisel hesaba (Premium + Business) bagli bir Bot API botu. Hesaba ozelden yazan
kullanicilarin mesajlari 'business_message' olarak duser; OpenAI cevap taslagi uretir;
owner onayindan sonra (veya OTOMATIK=1 ise dogrudan) hesap adina cevap gonderilir.

Userbot DEGIL -> Telegram'in resmi ozelligi, spam/otomasyon ban riski yok.
"""
import os
import html
import json
import uuid
import base64
import asyncio
import logging
import datetime

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    BusinessConnectionHandler,
    filters,
)

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OWNER_ID = int(os.environ["OWNER_ID"])
_OTOMATIK_VARSAYILAN = os.environ.get("OTOMATIK", "0") == "1"
# Calisma-zamani ayar dosyasi (/otomatik komutu ile degisir, restart'ta korunur)
AYAR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "durum.json")


def _ayar_yukle():
    """Kalici otomatik durumunu oku; dosya yoksa .env varsayilanini kullan."""
    try:
        import json as _json
        with open(AYAR_PATH, "r", encoding="utf-8") as f:
            return {"otomatik": bool(_json.load(f).get("otomatik", _OTOMATIK_VARSAYILAN))}
    except (OSError, ValueError):
        return {"otomatik": _OTOMATIK_VARSAYILAN}


def _ayar_kaydet():
    try:
        import json as _json
        with open(AYAR_PATH, "w", encoding="utf-8") as f:
            _json.dump(_ayar, f)
    except OSError as e:
        logger.exception(e)


_ayar = _ayar_yukle()

SISTEM_PROMPT = (
    "Sen bir Telegram destek asistanisin ve mesajlar isletme sahibinin KENDI hesabindan "
    "gonderiliyor. Yani musteri ZATEN dogru kisiyle konusuyor — sen hesap sahibisin. Musteri "
    "mesajina KISA, samimi, net Turkce cevap yaz (1-4 cumle). Urun, fiyat ve komut bilgisi icin "
    "yalnizca asagidaki BOT BILGILERI bolumune sadik kal.\n"
    "ONEMLI: Musteriyi baska bir hesaba ya da gelistiriciye YONLENDIRME — bu hesap zaten dogru "
    "yer. Emin olmadigin, cozemedigin ya da elle bakilmasi gereken bir sey olursa 'not aldim, "
    "kontrol edip en kisa surede donerim' gibi soyle. Uydurma. Kufreden/sinirli musteriye sakin ve "
    "ozur dileyen bir tonla yaklas, savunmaya gecme."
)

# Duzenlenebilir bot bilgi dosyasi (bot listesi, fiyat, komut). Her taslakta okunur -
# duzenleyip kaydedince restart gerekmez.
_DIZIN = os.path.dirname(os.path.abspath(__file__))
BILGI_PATH = os.path.join(_DIZIN, "bilgi.md")
if not os.path.exists(BILGI_PATH):
    BILGI_PATH = os.path.join(_DIZIN, "bilgi.ornek.md")
# Musteri basina saklanacak son mesaj sayisi (gelen + gonderilen). Bağlam icin.
MAX_GECMIS = 40
# Musteri basina GUNLUK gorsel (vision) hakki — spam ile OpenAI bakiyesini bosaltmayi engeller.
GORSEL_LIMIT = int(os.environ.get("GORSEL_LIMIT", "3"))
# Bolunmus mesajlari birlestirme: son mesajdan sonra bu kadar saniye yeni mesaj gelmezse isle.
BEKLEME_SN = float(os.environ.get("BEKLEME_SN", "6"))
# Mesajdan bu kadar saniye sonra "yazıyor..." gostergesi baslar (cevap gidene kadar surer).
YAZIYOR_GECIKME = float(os.environ.get("YAZIYOR_GECIKME", "5"))

# Onay bekleyen taslaklar: token -> (business_connection_id, musteri_chat_id, taslak, musteri_ad)
bekleyen = {}
# En son aktif business connection id (log/bilgi amacli)
_aktif_baglanti = {"id": None}
# Musteri chat_id -> [{"role": "user"/"assistant", "content": ...}] son MAX_GECMIS mesaj.
# Bellekte tutulur (restart'ta sifirlanir).
gecmis = {}
# Gorsel sayaci: chat_id -> {"gun": "YYYY-MM-DD", "adet": n} (gunluk sifirlanir).
_gorsel_sayac = {}
# Bolunmus mesaj tamponu: chat_id -> {"parcalar":[str], "resimler":[(bytes,url)], "bc_id","ad","task"}
_tampon = {}
# "yazıyor..." dongu gorevleri: chat_id -> asyncio.Task
_yaziyor_task = {}

_oai = AsyncOpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and AsyncOpenAI) else None


def _bilgi_oku():
    try:
        with open(BILGI_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _gecmis_ekle(chat_id, role, icerik):
    if not icerik:
        return
    liste = gecmis.setdefault(chat_id, [])
    liste.append({"role": role, "content": icerik})
    if len(liste) > MAX_GECMIS:
        del liste[: len(liste) - MAX_GECMIS]


def _gorsel_hakki_var(chat_id):
    """Bu musterinin bugun gorsel hakki kaldi mi? (gunluk GORSEL_LIMIT)."""
    bugun = datetime.date.today().isoformat()
    s = _gorsel_sayac.get(chat_id)
    if not s or s.get("gun") != bugun:
        return GORSEL_LIMIT > 0
    return s["adet"] < GORSEL_LIMIT


def _gorsel_kullan(chat_id):
    bugun = datetime.date.today().isoformat()
    s = _gorsel_sayac.get(chat_id)
    if not s or s.get("gun") != bugun:
        s = {"gun": bugun, "adet": 0}
        _gorsel_sayac[chat_id] = s
    s["adet"] += 1


async def _yaziyor_dongu(chat_id, context, bc_id):
    """YAZIYOR_GECIKME sn bekle, sonra cevap gonderilene (task iptal) kadar 'yazıyor...' yenile."""
    try:
        await asyncio.sleep(YAZIYOR_GECIKME)
        while True:
            try:
                await context.bot.send_chat_action(
                    chat_id, "typing", business_connection_id=bc_id
                )
            except Exception as e:
                logger.exception(e)
            await asyncio.sleep(4)  # chat action ~5sn surer, ondan once yenile
    except asyncio.CancelledError:
        return


def _yaziyor_baslat(chat_id, context, bc_id):
    eski = _yaziyor_task.get(chat_id)
    if eski:
        eski.cancel()
    _yaziyor_task[chat_id] = context.application.create_task(
        _yaziyor_dongu(chat_id, context, bc_id)
    )


def _yaziyor_durdur(chat_id):
    t = _yaziyor_task.pop(chat_id, None)
    if t:
        t.cancel()


async def _resim_indir(context, msg):
    """business_message'taki foto/image-mime dosyayi indir. (ham_bytes, data_url) dondur; yoksa (None, None)."""
    file_id = None
    mime = "image/jpeg"
    if msg.photo:
        file_id = msg.photo[-1].file_id  # en buyuk boyut
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
        mime = msg.document.mime_type
    if not file_id:
        return None, None
    try:
        f = await context.bot.get_file(file_id)
        ba = bytes(await f.download_as_bytearray())
        return ba, f"data:{mime};base64," + base64.b64encode(ba).decode()
    except Exception as e:
        logger.exception(e)
        return None, None


JSON_TALIMAT = (
    "\n\n=== CIKTI BICIMI ===\n"
    "Yanitini SADECE su JSON nesnesi olarak ver (baska metin yok):\n"
    '{"cevap": "<musteriye gidecek Turkce cevap>", '
    '"spam": true veya false, '
    '"bug": true veya false, '
    '"teshis": "<bug true ise gelistirici icin teknik teshis>"}\n'
    "- spam: mesaj reklam, alakasiz tanitim, dolandiricilik ya da anlamsiz/rastgele ise true "
    "(bu durumda cevap bos olabilir, gonderilmeyecek). Gercek destek mesaji ise false.\n"
    "- bug: SADECE gercek bir yazilim arizasi gibiyse true — botun verdigi bir hata mesaji, "
    "cokme, odeme alindi ama lisans tanimlanmadi, ya da dogru kurulum/yetkiye ragmen suren "
    "beklenmedik davranis. Kurulum/yetki eksigi, kullanim sorusu, fiyat, kullanici hatasi ya da "
    "emin olamadigin durum ise false (bunlari cevapta yonlendir, escalate etme).\n"
    "- teshis: bug true ise BOS BIRAKMA. Gelistirici icin su 3 seyi yaz: (1) olası neden bir cumle, "
    "(2) hangi bot ve muhtemelen nerede (dosya/komut/fonksiyon tahmini), (3) kontrol edilecek/denenecek "
    "somut adim ya da olası duzeltme yonu. bug false ise bos string."
)


async def _taslak_uret(chat_id, guncel_icerik):
    """Gecmis + su anki mesaj ile OpenAI'den {cevap, bug, teshis} uret. Key yok/hata ise None.
    guncel_icerik: str veya OpenAI multimodal content listesi."""
    if _oai is None:
        return None
    sistem = SISTEM_PROMPT
    bilgi = _bilgi_oku()
    if bilgi:
        sistem = f"{SISTEM_PROMPT}\n\n=== BOT BILGILERI (kaynak) ===\n{bilgi}"
    sistem += JSON_TALIMAT
    mesajlar = (
        [{"role": "system", "content": sistem}]
        + gecmis.get(chat_id, [])
        + [{"role": "user", "content": guncel_icerik}]
    )
    try:
        r = await _oai.chat.completions.create(
            model=OPENAI_MODEL,
            max_completion_tokens=700,
            response_format={"type": "json_object"},
            messages=mesajlar,
        )
        ham = r.choices[0].message.content or ""
    except Exception as e:
        logger.exception(e)
        return None
    try:
        d = json.loads(ham)
        spam = bool(d.get("spam"))
        cevap = (d.get("cevap") or "").strip()
        if not cevap and not spam:
            return None
        return {"cevap": cevap, "spam": spam, "bug": bool(d.get("bug")),
                "teshis": (d.get("teshis") or "").strip()}
    except (ValueError, AttributeError):
        # JSON gelmezse ham metni cevap say (spam/bug tespiti atlanir)
        t = ham.strip()
        return {"cevap": t, "spam": False, "bug": False, "teshis": ""} if t else None


async def baglanti_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hesap bota baglandiginda/koptugunda gelir."""
    bc = update.business_connection
    if bc is None:
        return
    _aktif_baglanti["id"] = bc.id
    logger.info("Business connection: id=%s enabled=%s", bc.id, bc.is_enabled)
    try:
        durum = "aktif ✅" if bc.is_enabled else "pasif ⛔"
        await context.bot.send_message(
            OWNER_ID,
            f"🔗 Destek botu hesabina baglandi ({durum}).\n"
            f"Artik hesabina ozelden gelen mesajlara taslak uretecegim.",
        )
    except Exception as e:
        logger.exception(e)


async def gelen_mesaj_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gelen musteri mesajini tampona koy; kisa bir bekleme sonrasi (bolunmus mesajlar
    birlesince) tek seferde isle. Gorsel indirme gunluk limitle sinirli."""
    msg = update.business_message
    if msg is None:
        return
    metin = (msg.text or msg.caption or "").strip()
    musteri_chat = msg.chat.id

    # Owner kendi yazdiysa: gecmise 'assistant' olarak kaydet ve bekleyen tamponu iptal et
    # (owner elle mudahale ediyor, bot cevap uretmesin).
    if msg.from_user and msg.from_user.id == OWNER_ID:
        _gecmis_ekle(musteri_chat, "assistant", metin)
        eski = _tampon.get(musteri_chat)
        if eski and eski.get("task"):
            eski["task"].cancel()
        _tampon.pop(musteri_chat, None)
        _yaziyor_durdur(musteri_chat)  # owner mudahale etti, gostergeyi kes
        return

    # Gorsel var mi? Gunluk limit dahilinde indir.
    resim = None
    limit_doldu = False
    if msg.photo or (msg.document and (msg.document.mime_type or "").startswith("image/")):
        if _gorsel_hakki_var(musteri_chat):
            rb, ru = await _resim_indir(context, msg)
            if ru:
                resim = (rb, ru)
                _gorsel_kullan(musteri_chat)
        else:
            limit_doldu = True

    if not metin and not resim and not limit_doldu:
        return

    t = _tampon.setdefault(
        musteri_chat,
        {"parcalar": [], "resimler": [], "bc_id": msg.business_connection_id, "ad": "?", "task": None},
    )
    t["bc_id"] = msg.business_connection_id
    if msg.from_user:
        t["ad"] = msg.from_user.full_name
    if metin:
        t["parcalar"].append(metin)
    if resim:
        t["resimler"].append(resim)
    if limit_doldu:
        t["parcalar"].append(
            "(Sistem notu: bu musteri gunluk gorsel limitini doldurdu; yeni gorseli inceleyemedim. "
            "Kibarca yarin tekrar deneyebilecegini ya da sorunu yazarak anlatabilecegini soyle.)"
        )

    # Debounce: onceki bekleyen isi iptal et, yeni bekleme baslat
    if t["task"]:
        t["task"].cancel()
    t["task"] = context.application.create_task(_isle_tampon(musteri_chat, context))

    # Otomatik modda: mesajdan YAZIYOR_GECIKME sn sonra "yazıyor..." baslasin (cevaba kadar)
    if _ayar["otomatik"]:
        _yaziyor_baslat(musteri_chat, context, msg.business_connection_id)


async def _isle_tampon(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Bekleme suresi dolunca tampondaki (birlesmis) mesaji tek seferde isle."""
    try:
        await asyncio.sleep(BEKLEME_SN)
    except asyncio.CancelledError:
        return  # yeni mesaj geldi, bu is iptal; yenisi calisacak

    t = _tampon.pop(chat_id, None)
    if not t:
        return
    metin = " ".join(p for p in t["parcalar"] if p).strip()
    resimler = t["resimler"]
    bc_id = t["bc_id"]
    musteri_ad = t["ad"]
    if not metin and not resimler:
        return

    if resimler:
        parcalar = [{"type": "text", "text": metin or "Musteri ekran goruntusu gonderdi. Incele ve yardimci ol."}]
        for _b, u in resimler:
            parcalar.append({"type": "image_url", "image_url": {"url": u}})
        guncel = parcalar
        ozet = (metin + " " if metin else "") + f"[{len(resimler)} gorsel gonderdi]"
    else:
        guncel = metin
        ozet = metin

    # Otomatik modda: cevap uretilirken native "Düşünüyor..." göstergesi (Bot API 10.0
    # sendMessageDraft, bos text = "Thinking..." placeholder; efemeral 30sn). business_connection_id
    # PTB imzasinda yok, api_kwargs ile geciyoruz.
    if _ayar["otomatik"]:
        try:
            await context.bot.send_message_draft(
                chat_id,
                draft_id=(abs(chat_id) % 2000000000) or 1,
                text="",
                api_kwargs={"business_connection_id": bc_id},
            )
        except Exception as e:
            logger.exception(e)

    sonuc = await _taslak_uret(chat_id, guncel)
    _gecmis_ekle(chat_id, "user", ozet)
    musteri_chat = chat_id

    if sonuc is None:
        _yaziyor_durdur(chat_id)
        # OpenAI yok/başarısız: draft 30sn'de kendiliginden kaybolur; owner'a haber ver
        await context.bot.send_message(
            OWNER_ID,
            f"📩 <b>{html.escape(musteri_ad)}</b>:\n{html.escape(metin)}\n\n"
            f"<i>(Taslak uretilemedi — OPENAI_API_KEY yok ya da hata. Kendin cevapla.)</i>",
            parse_mode="html",
        )
        return

    # Spam: cevap verme, gostergeyi kes, sessizce birak (log)
    if sonuc.get("spam"):
        _yaziyor_durdur(chat_id)
        logger.info("spam (cevap verilmedi) chat=%s: %s", chat_id, metin[:80])
        return

    taslak = sonuc["cevap"]

    # Olası BUG ise owner'a escalation: rapor + ekran goruntusu + teshis (mod ne olursa olsun)
    if sonuc.get("bug"):
        teshis_metni = sonuc.get("teshis") or (
            "(Otomatik teşhis üretilemedi. Gerçek çözüm için mesajı Claude'a getir — kodu okuyup düzeltir.)"
        )
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ <b>Olası BUG bildirimi</b> — <b>{html.escape(musteri_ad)}</b>\n\n"
                f"💬 Müşteri: {html.escape(metin) or '(sadece görsel)'}\n\n"
                f"🔧 <b>Ön teşhis (kodu görmeden tahmin):</b>\n{html.escape(teshis_metni)}\n\n"
                f"<i>Gerçek düzeltme için bu bug'ı Claude'a getir. Kodu otomatik değiştirmedim.</i>",
                parse_mode="html",
            )
            for _b, _u in resimler:
                if _b:
                    await context.bot.send_photo(
                        OWNER_ID, photo=_b, caption=f"📷 {html.escape(musteri_ad)} ekran görüntüsü"
                    )
        except Exception as e:
            logger.exception(e)

    if _ayar["otomatik"]:
        _yaziyor_durdur(musteri_chat)  # cevap gidiyor, gostergeyi kes
        try:
            await context.bot.send_message(
                chat_id=musteri_chat, text=taslak, business_connection_id=bc_id
            )
            _gecmis_ekle(musteri_chat, "assistant", taslak)
            await context.bot.send_message(
                OWNER_ID,
                f"🤖 Otomatik cevaplandi → <b>{html.escape(musteri_ad)}</b>\n"
                f"Soru: {html.escape(metin)}\nCevap: {html.escape(taslak)}",
                parse_mode="html",
            )
        except Exception as e:
            logger.exception(e)
            await context.bot.send_message(
                OWNER_ID, f"🔴 Otomatik gonderim hatasi: {html.escape(str(e))}", parse_mode="html"
            )
        return

    # Onayli akis: taslagi owner'a butonlarla sun
    _yaziyor_durdur(musteri_chat)
    tok = uuid.uuid4().hex[:12]
    bekleyen[tok] = (bc_id, musteri_chat, taslak, musteri_ad)
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Gönder", callback_data=f"gonder:{tok}"),
            InlineKeyboardButton("❌ İptal", callback_data=f"iptal:{tok}"),
        ]]
    )
    await context.bot.send_message(
        OWNER_ID,
        f"📩 <b>{html.escape(musteri_ad)}</b>:\n{html.escape(metin)}\n\n"
        f"🧠 <b>Taslak:</b>\n{html.escape(taslak)}",
        reply_markup=kb,
        parse_mode="html",
    )


async def buton_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        return
    try:
        aksiyon, tok = q.data.split(":", 1)
    except ValueError:
        return
    veri = bekleyen.pop(tok, None)
    if veri is None:
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        return
    bc_id, musteri_chat, taslak, musteri_ad = veri
    govde = q.message.text_html if q.message else ""
    if aksiyon == "gonder":
        try:
            await context.bot.send_message(
                chat_id=musteri_chat, text=taslak, business_connection_id=bc_id
            )
            _gecmis_ekle(musteri_chat, "assistant", taslak)
            await q.edit_message_text(govde + "\n\n✅ <b>Gönderildi.</b>", parse_mode="html")
        except Exception as e:
            logger.exception(e)
            await q.edit_message_text(
                govde + f"\n\n🔴 Hata: {html.escape(str(e))}", parse_mode="html"
            )
    else:
        await q.edit_message_text(govde + "\n\n❌ <b>İptal</b> — kendin yaz.", parse_mode="html")


def _durum_metni():
    mod = "🟢 OTOMATIK (onaysiz direkt gonderiyorum)" if _ayar["otomatik"] else "🟡 ONAYLI (taslagi sana yolluyorum)"
    return (
        f"Durum: {mod}\nModel: {OPENAI_MODEL}\nOpenAI: {'bagli' if _oai else 'KAPALI (key yok)'}\n\n"
        "/otomatik — otomatik/onayli modu degistir\n/durum — bu bilgiyi goster"
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text("Bu bot yalnizca sahibinin destek asistanidir.")
        return
    await update.effective_message.reply_text(
        "Destek asistani aktif. Hesabini baglamak icin:\n"
        "Telegram → Ayarlar → Business → Chatbots → @<bu_bot> → izin ver.\n\n"
        "Baglaninca ozelden gelen mesajlara taslak uretip buraya onaya gonderecegim.\n\n"
        + _durum_metni()
    )


async def otomatik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    # /otomatik ac | kapat | (argsız: degistir)
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("ac", "aç", "on", "1", "acik", "açık"):
        _ayar["otomatik"] = True
    elif arg in ("kapat", "off", "0", "kapali", "kapalı"):
        _ayar["otomatik"] = False
    else:
        _ayar["otomatik"] = not _ayar["otomatik"]
    _ayar_kaydet()
    yeni = "🟢 OTOMATIK açık — cevaplar onaysız direkt senin hesabından gidecek." if _ayar["otomatik"] \
        else "🟡 ONAYLI mod — taslakları sana yollayacağım, sen 'Gönder' deyince gidecek."
    await update.effective_message.reply_text(yeni)


async def durum_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.effective_message.reply_text(_durum_metni())


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("otomatik", otomatik_cmd))
    app.add_handler(CommandHandler("durum", durum_cmd))
    app.add_handler(BusinessConnectionHandler(baglanti_handler))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, gelen_mesaj_handler))
    app.add_handler(CallbackQueryHandler(buton_handler))
    logger.info("Destek botu basladi (model=%s, otomatik=%s, openai=%s)",
                OPENAI_MODEL, _ayar["otomatik"], bool(_oai))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
