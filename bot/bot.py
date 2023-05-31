import logging
import asyncio
import traceback
import html
import json
import tempfile
from pathlib import Path
from datetime import datetime
import telegram
from telegram import (
    Update,
    InputMediaDocument,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode, ChatAction
import config
import database
import openai_utils

db = database.Database()
logger = logging.getLogger(__name__)
bb = asyncio.create_task
bcs = asyncio.ensure_future
loop = asyncio.get_event_loop()
sleep = asyncio.sleep
chat_locks = {}
chat_tasks = {}
apis_vivas = []

async def obtener_vivas():
    from apistatus import estadosapi
    global apis_vivas
    apis_vivas = await estadosapi()

def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]

async def handle_chat_task(chat, lang, task, update):
    async with chat_locks[chat.id]:
        chat_tasks[chat.id] = task
        try:
            await acquiresemaphore(chat=chat)
            await task
        except asyncio.CancelledError:
            await update.effective_chat.send_message(f'{config.lang["mensajes"]["cancelado"][lang]}', parse_mode=ParseMode.HTML)
            await releasemaphore(chat=chat)
        else:
            await releasemaphore(chat=chat)
            pass
        finally:
            if chat.id in chat_tasks:
                del chat_tasks[chat.id]
                await releasemaphore(chat=chat)
async def acquiresemaphore(chat):
    lock = chat_locks.get(chat.id)
    if lock is None:
        lock = asyncio.Lock()  # Inicializa un nuevo bloqueo si no existe
        chat_locks[chat.id] = lock
    await lock.acquire()
async def releasemaphore(chat):
    lock = chat_locks.get(chat.id)
    if lock and lock.locked():
        lock.release()

async def is_previous_message_not_answered_yet(chat, lang, update: Update):
    semaphore = chat_locks.get(chat.id)
    if semaphore and semaphore.locked():
        text = f'{config.lang["mensajes"]["mensaje_semaforo"][lang]}'
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    else:
        return False

async def is_bot_mentioned(update: Update, context: CallbackContext):
    message=update.message
    try:
        if message.chat.type == "private":
            return True

        if message.text is not None and ("@" + context.bot.username) in message.text:
            return True
        
        if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
            return True
    except:
        return True
    else:
        return False

async def start_handle(update: Update, context: CallbackContext):
    lang = await lang_check(update, context)
    reply_text = f'{config.lang["mensajes"]["mensaje_bienvenido"][lang]}🤖\n\n'
    reply_text += f'{config.lang["mensajes"]["mensaje_ayuda"][lang]}'
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)

async def new_dialog_handle(update: Update, context: CallbackContext, chat=None, lang=None):
    if not chat:
        chat  = await chat_check(update, context)
    if not lang:
        lang = await lang_check(update, context, chat)
    if await is_previous_message_not_answered_yet(chat, lang, update): return
    api_actual = db.get_chat_attribute(chat.id, 'current_api')
    modelo_actual = db.get_chat_attribute(chat.id, 'current_model')
    mododechat_actual=db.get_chat_attribute(chat.id, 'current_chat_mode')
    # Verificar si hay valores inválidos en el usuario
    if (mododechat_actual not in config.chat_mode["available_chat_mode"] or api_actual not in apis_vivas or modelo_actual not in config.model["available_model"]):
        db.reset_chat_attribute(chat.id)
        await update.effective_chat.send_message(f'{config.lang["mensajes"]["reset_chat_attributes"][lang]}')
    modelos_disponibles=config.api["info"][api_actual]["available_model"]
    api_actual_name=config.api["info"][api_actual]["name"]
    # Verificar si el modelo actual es válido en la API actual
    if modelo_actual not in modelos_disponibles:
        db.set_chat_attribute(chat.id, "current_model", modelos_disponibles[1])
        await update.effective_chat.send_message(f'{config.lang["mensajes"]["model_no_compatible"][lang].format(api_actual_name=api_actual_name, new_model_name=config.model["info"][db.get_chat_attribute(chat.id, "current_model")]["name"])}')
    db.new_dialog(chat.id)
    db.delete_all_dialogs_except_current(chat.id)
    #Bienvenido!
    await update.effective_chat.send_message(f"{config.chat_mode['info'][db.get_chat_attribute(chat.id, 'current_chat_mode')]['welcome_message'][lang]}", parse_mode=ParseMode.HTML)
    db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    await releasemaphore(chat=chat)

async def lang_check(update: Update, context: CallbackContext, chat=None, lang=None):
    if chat is None:
        chat = await chat_check(update, context)
    if lang is None:
        if db.chat_exists(chat.id):
            lang = db.get_chat_attribute(chat.id, "current_lang")
        else:
            if update.effective_user.language_code in config.lang["available_lang"]:
                lang = update.effective_user.language_code
            else:
                lang = str(config.pred_lang)
    return lang
async def chat_check(update: Update, context: CallbackContext, chat=None, lang=None):
    if not chat:
        if update.message:
            chat = update.message.chat
        elif update.callback_query:
            chat = update.callback_query.message.chat
    if not db.chat_exists(chat.id):
        lang = await lang_check(update, context, chat)
        db.add_chat(chat.id, lang)
        await cambiar_idioma(update, context, chat, lang)
        db.new_dialog(chat.id)
    if chat.id not in chat_locks:
        chat_locks[chat.id] = asyncio.Semaphore(1)
    return chat

async def cambiar_idioma(update: Update, context: CallbackContext, chat=None, lang=None):
    if not chat:
        chat = await chat_check(update, context)
    if not lang:
        lang = await lang_check(update, context, chat)
    else:
        db.set_chat_attribute(chat.id, "current_lang", lang)
    # commandos = [
    #     BotCommand("/new", f'{config.lang["commands"]["new"][lang]}'),
    #     BotCommand("/chat_mode", f'{config.lang["commands"]["chat_mode"][lang]}'),
    #     BotCommand("/retry", f'{config.lang["commands"]["retry"][lang]}'),
    #     BotCommand("/model", f'{config.lang["commands"]["model"][lang]}'),
    #     BotCommand("/api", f'{config.lang["commands"]["api"][lang]}'),
    #     BotCommand("/img", f'{config.lang["commands"]["img"][lang]}'),
    #     BotCommand("/lang", f'{config.lang["commands"]["lang"][lang]}'),
    #     BotCommand("/help", f'{config.lang["commands"]["help"][lang]}')
    # ]
    await update.effective_chat.send_message(f'{config.lang["info"]["bienvenida"][lang]}')
    return lang

async def help_handle(update: Update, context: CallbackContext):
    chat = await chat_check(update, context)
    lang = await lang_check(update, context, chat)
    await update.message.reply_text(config.lang["mensajes"]["mensaje_ayuda"][lang], parse_mode=ParseMode.HTML)

async def help_group_chat_handle(update: Update, context: CallbackContext):
    chat = await chat_check(update, context)
    lang = await lang_check(update, context, chat)    
    text = config.lang["mensajes"]["ayuda_grupos"][lang].format(bot_username="@" + context.bot.username)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    await update.message.reply_video(config.help_group_chat_video_path)

async def retry_handle(update: Update, context: CallbackContext, chat=None, lang=None):
    if not chat:
        chat = await chat_check(update, context)
    if not lang:
        lang = await lang_check(update, context, chat)
    if await is_previous_message_not_answered_yet(chat, lang, update): return
    dialog_messages = db.get_dialog_messages(chat.id, dialog_id=None)
    if len(dialog_messages) == 0:
        await releasemaphore(chat=chat)
        await update.message.reply_text(f'{config.lang["mensajes"]["no_retry_mensaje"][lang]}')
        return
    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(chat.id, dialog_messages, dialog_id=None)  # last message was removed from the context
    db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    await releasemaphore(chat=chat)
    await message_handle(chat, lang, update, context, _message=last_dialog_message["user"])

async def check_message(update: Update, _message=None):
    raw_msg = _message or update.effective_message
    if isinstance(raw_msg, str):
        _message = raw_msg
        raw_msg = update.effective_chat
    elif hasattr(raw_msg, 'text'):
        _message = raw_msg.text
    else:
        _message = _message
    return raw_msg, _message

async def add_dialog_message(chat, new_dialog_message):
    db.set_dialog_messages(
        chat.id,
        db.get_dialog_messages(chat.id, dialog_id=None) + [new_dialog_message],
        dialog_id=None
    )
    return

async def message_handle_wrapper(update, context):
    chat = await chat_check(update, context)
    lang = await lang_check(update, context, chat)
    # check if bot was mentioned (for groups)
    if not await is_bot_mentioned(update, context): return
    if await is_previous_message_not_answered_yet(chat, lang, update): return
    task = bb(message_handle(chat, lang, update, context))
    bcs(handle_chat_task(chat, lang, task, update))

async def message_handle(chat, lang, update: Update, context: CallbackContext, _message=None):
    if _message:
        raw_msg = _message
    else:
        raw_msg, _message = await check_message(update, _message)
        try:
            if raw_msg.entities:
                urls = []
                for entity in raw_msg.entities:
                    if entity.type == 'url':
                        url_add = raw_msg.text[entity.offset:entity.offset+entity.length]
                        if "http://" in url_add or "https://" in url_add:
                            urls.append(raw_msg.text[entity.offset:entity.offset+entity.length])
                        else:
                            pass
                if urls:
                    await releasemaphore(chat=chat)
                    task = bb(url_handle(chat, lang, update, context, urls))
                    bcs(handle_chat_task(chat, lang, task, update))
                    return
        except AttributeError:
            pass
    
    dialog_messages = db.get_dialog_messages(chat.id, dialog_id=None)
    if (datetime.now() - db.get_chat_attribute(chat.id, "last_interaction")).seconds > config.dialog_timeout and len(dialog_messages) > 0:
        if config.timeout_ask == "True":
            await ask_timeout_handle(chat, lang, update, context, _message)
            return
        else:
            await new_dialog_handle(update, context, chat, lang)
            await update.effective_chat.send_message(f'{config.lang["mensajes"]["timeout_ask_false"][lang].format(chatmode=config.chat_mode["info"][chat_mode]["name"][lang])}', parse_mode=ParseMode.HTML)

    #remove bot mention (in group chats)
    if chat.type != "private":
        _message = _message.replace("@" + context.bot.username, "").strip()
        _message = f"{raw_msg.from_user.first_name}@{raw_msg.from_user.username}: {_message}"
    chat_mode = db.get_chat_attribute(chat.id, "current_chat_mode")
    current_model = db.get_chat_attribute(chat.id, "current_model")
    #await message_handle_fn(update, context, _message, chat, lang, dialog_messages, chat_mode, current_model)
    await releasemaphore(chat=chat)
    task = bb(message_handle_fn(update, context, _message, chat, lang, dialog_messages, chat_mode, current_model))
    bcs(handle_chat_task(chat, lang, task, update))


async def message_handle_fn(update, context, _message, chat, lang, dialog_messages, chat_mode, current_model):
    # in case of CancelledError
    try:
        # send placeholder message to chat
        placeholder_message = await update.effective_chat.send_message("🤔")
        # send typing action
        if chat:
            await update.effective_chat.send_action(ChatAction.TYPING)
        if _message is None or len(_message) == 0:
            await update.effective_chat.send_message(f'{config.lang["mensajes"]["message_empty_handle"][lang]}', parse_mode=ParseMode.HTML)
            return
        parse_mode = {
            "html": ParseMode.HTML,
            "markdown": ParseMode.MARKDOWN
        }[config.chat_mode["info"][chat_mode]["parse_mode"]]
        chatgpt_instance = openai_utils.ChatGPT(model=current_model)
        gen = chatgpt_instance.send_message(_message, chat.id, lang, dialog_messages, chat_mode)     
        prev_answer = ""
        async for status, gen_answer in gen:                                                         
            answer = gen_answer[:4096]  # telegram message limit                                     
            # update only when 100 new symbols are ready                                             
            if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":                    
                continue                                                                             
            try:                                                                                     
                await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat.id, message_id=placeholder_message.message_id, parse_mode=parse_mode)                                
            except telegram.error.BadRequest as e:                                                   
                if str(e).startswith("Message is not modified"):                                     
                    continue                                                                         
                else:                                                                                
                    await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat.id, message_id=placeholder_message.message_id)                                                       
            await sleep(0.05)  # wait a bit to avoid flooding                                 
            prev_answer = answer
        # update chat data
        db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
        new_dialog_message = {"user": _message, "bot": answer, "date": datetime.now()}
        await add_dialog_message(chat, new_dialog_message)
        await releasemaphore(chat=chat)
    except Exception as e:
        logger.error(f'{config.lang["errores"]["error"][lang]}: {e}')
        await releasemaphore(chat=chat)
        await update.effective_chat.send_message(f'{config.lang["errores"]["error"][lang]}: {e}')
        return
    if chat_mode == "imagen":
        await generate_image_wrapper(update, context, _message=answer, chat=chat, lang=lang)

async def clean_text(doc, name):
    import re
    doc = re.sub(r'^\n', '', doc) 
    doc = re.sub(r'\n+', r' ', doc) # Reemplaza saltos de línea dentro de párrafos por un espacio  
    doc = re.sub(r' {2,}', ' ', doc) # Reemplaza dos o más espacios con uno solo
    doc = re.sub(r'\s+', ' ', doc).strip()
    #doc = "\n".join(line.strip() for line in doc.split("\n"))
    doc_text = f'[{name}: {doc}]'
    return doc_text

async def url_handle(chat, lang, update, context, urls):
    chat = await chat_check(update, context)
    import requests
    from bs4 import BeautifulSoup
    import warnings
    warnings.filterwarnings("ignore")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36 Edg/91.0.864.54"
    }
    for url in urls:
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            if len(response.content) > config.url_max_size * (1024 * 1024):
                raise Exception(f'{config.lang["errores"]["url_size_exception"][lang]}')
            soup = BeautifulSoup(response.content, "html.parser")
            body_tag = soup.body
            if body_tag:
                doc = body_tag.get_text('\n')
            else:
                # Si no hay etiqueta <body>, obtener todo el contenido de la página
                doc = soup.get_text('\n')
            doc_text = await clean_text(doc, name=url)
            new_dialog_message = {"url": doc_text, "user": ".", "date": datetime.now()}
            await add_dialog_message(chat, new_dialog_message)
            text = f'{config.lang["mensajes"]["url_anotado_ask"][lang]}'
        except Exception as e:
            text = f'{config.lang["errores"]["url_size_limit"][lang]}: {e}.'
            logger.error(text)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    await releasemaphore(chat=chat)

async def document_handle(chat, lang, update, context):
    document = update.message.document
    file_size_mb = document.file_size / (1024 * 1024)
    if file_size_mb <= config.file_max_size:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir = Path(tmp_dir)
            ext = document.file_name.split(".")[-1]
            doc_path = tmp_dir / Path(document.file_name)
            # download
            doc_file = await context.bot.get_file(document.file_id)
            await doc_file.download_to_drive(doc_path)
            if "pdf" in ext:
                pdf_file = open(doc_path, 'rb')
                import PyPDF2
                read_pdf = PyPDF2.PdfReader(pdf_file)
                doc = ''
                paginas = len(read_pdf.pages)
                if paginas > config.pdf_page_lim:
                    text = f'{config.lang["errores"]["pdf_pages_limit"][lang].format(paginas=paginas, pdf_page_lim=config.pdf_page_lim)}'
                    paginas = config.pdf_page_lim - 1
                    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
                for i in range(paginas):
                    text = read_pdf.pages[i].extract_text()
                    text = text.replace(".\n", "|n_parraf|")  
                    paras = text.split("|n_parraf|")
                    parafo_count = 1
                    for para in paras:
                        if len(para) > 3:
                            doc += f'{config.lang["metagen"]["paginas"][lang]}{i+1}_{config.lang["metagen"]["parrafos"][lang]}{parafo_count}: {para}\n\n'      
                            parafo_count += 1
            else:
                with open(doc_path, 'r') as f:
                    doc = f.read()
            doc_text = await clean_text(doc, name=document.file_name)
            new_dialog_message = {"documento": doc_text, "user": ".", "date": datetime.now()}
            await add_dialog_message(chat, new_dialog_message)
            text = f'{config.lang["mensajes"]["document_anotado_ask"][lang]}'
            db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    else:
        text = f'{config.lang["errores"]["document_size_limit"][lang].replace("{file_size_mb}", f"{file_size_mb:.2f}").replace("{file_max_size}", str(config.file_max_size))}'
    await releasemaphore(chat=chat)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
async def document_wrapper(update, context):
    chat = await chat_check(update, context)
    lang = await lang_check(update, context, chat)
    if not await is_bot_mentioned(update, context): return
    if await is_previous_message_not_answered_yet(chat, lang, update): return
    task = bb(document_handle(chat, lang, update, context))
    bcs(handle_chat_task(chat, lang, task, update))

async def ocr_image(chat, lang, update, context):
    image = update.message.photo[-1]
    from PIL import Image
    import pytesseract
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Descargar y convertir a MP3
        tmp_dir = Path(tmp_dir)
        #ext = image.mime_type
        #ext = mimetypes.guess_extension(ext)
        img_path = tmp_dir / Path("ocrimagen.jpg")
        image_file = await context.bot.get_file(image.file_id)
        await image_file.download_to_drive(img_path)
        #import cv2
        #img = cv2.imread(str(img_path))

        # Redimensionar la imagen a la mitad
        #img = cv2.resize(img, None, fx=0.5, fy=0.5)

        # Aplicar umbralización
        #gray_img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        #_, thresh_img = cv2.threshold(gray_img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # Reducción de ruido 
        #denoised_img = cv2.fastNlMeansDenoisingColored(thresh_img, None, 10, 10, 7, 21)

        # Mejora de contraste
        #enhanced_img = cv2.equalizeHist(denoised_img)

        # Obtener el texto de la imagen utilizando pytesseract
        # Carga la imagen
        imagen = Image.open(str(img_path))
        imagen.info['dpi'] = (300, 300)
        


        # Detecta el idioma de la imagen usando el parámetro 'lang' y el valor 'osd' (Oriented Script Detection)
        #datos_osd = pytesseract.image_to_osd(imagen)
        #idioma_detectado = datos_osd.split("Script: ")[1].split("\n")[0]

        # Lee el texto de la imagen usando el idioma detectado

        texto = pytesseract.image_to_string(str(img_path))
        
        db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    new_dialog_message = {"user": f'{config.lang["metagen"]["transcripcion_imagen"][lang]}: "{texto}"', "date": datetime.now()}
    await update.message.reply_text(f'{config.lang["mensajes"]["image_ocr_ask"][lang].format(ocresult=texto)}')
    await add_dialog_message(chat, new_dialog_message)
    await releasemaphore(chat=chat)
async def ocr_image_wrapper(update, context):
    chat = await chat_check(update, context)
    lang = await lang_check(update, context, chat)
    if not await is_bot_mentioned(update, context): return
    if await is_previous_message_not_answered_yet(chat, lang, update): return
    task = bb(ocr_image(chat, lang, update, context))
    bcs(handle_chat_task(chat, lang, task, update))

async def transcribe_message_handle(chat, lang, update, context):
    # Procesar sea voz o audio         
    if update.message.voice:
        audio = update.message.voice     
    elif update.message.audio:
        audio = update.message.audio
    file_size_mb = audio.file_size / (1024 * 1024)
    if file_size_mb <= config.audio_max_size:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Descargar y convertir a MP3
            tmp_dir = Path(tmp_dir)
            ext = audio.mime_type
            import mimetypes
            ext = mimetypes.guess_extension(ext)
            doc_path = tmp_dir / Path("tempaudio" + ext)

            # download
            voice_file = await context.bot.get_file(audio.file_id)
            await voice_file.download_to_drive(doc_path)

            # convert to mp3
            mp3_file_path = tmp_dir / "voice.mp3"
            from pydub import AudioSegment
            AudioSegment.from_file(doc_path).export(mp3_file_path, format="mp3")

            # Transcribir
            with open(mp3_file_path, "rb") as f:
                await releasemaphore(chat=chat)
                transcribed_text = await openai_utils.transcribe_audio(chat.id, f)

        # Enviar respuesta            
        text = f"🎤 {transcribed_text}"
        db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    else:
        text = f'{config.lang["errores"]["audio_size_limit"][lang].format(audio_max_size=config.audio_max_size)}'
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    await releasemaphore(chat=chat)
    await message_handle(chat, lang, update, context, _message=transcribed_text)
async def transcribe_message_wrapper(update, context):
    chat = await chat_check(update, context)
    lang = await lang_check(update, context, chat)
    if not await is_bot_mentioned(update, context): return
    if await is_previous_message_not_answered_yet(chat, lang, update): return
    task = bb(transcribe_message_handle(chat, lang, update, context))
    bcs(handle_chat_task(chat, lang, task, update))

async def generate_image_handle(chat, lang, update: Update, context: CallbackContext, _message=None):
    if _message:
        prompt = _message
    else:
        if not context.args:
            await update.message.reply_text(f'{config.lang["mensajes"]["genimagen_noargs"][lang]}', parse_mode=ParseMode.HTML)
            await releasemaphore(chat=chat)
            return
        else:
            prompt = ' '.join(context.args)
    if prompt == None:
        await update.message.reply_text(f'{config.lang["mensajes"]["genimagen_notext"][lang]}', parse_mode=ParseMode.HTML)
        await releasemaphore(chat=chat)
        return
    import openai
    try:
        await releasemaphore(chat=chat)
        await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
        image_urls = await openai_utils.generate_images(prompt, chat.id)
    except (openai.error.APIError, openai.error.InvalidRequestError) as e:
        if "Request has inappropriate content!" in str(e) or "Your request was rejected as a result of our safety system." in str(e):
            text = f'{config.lang["errores"]["genimagen_rejected"][lang]}'
        else:
            text = f'{config.lang["errores"]["genimagen_other"][lang]}'
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        await releasemaphore(chat=chat)
        return
    except telegram.error.BadRequest as e:
        text = f'{config.lang["errores"]["genimagen_badrequest"][lang]}'
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        await releasemaphore(chat=chat)
        return

    image_group=[]
    document_group=[]
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    for i, image_url in enumerate(image_urls):
        image = InputMediaPhoto(image_url)
        image_group.append(image)
        document = InputMediaDocument(image_url, parse_mode=ParseMode.HTML, filename=f"imagen_{i}.png")
        document_group.append(document)
    try:
        await update.message.reply_media_group(image_group)
        await update.message.reply_media_group(document_group)
    except "Timed out" in telegram.error.TimedOut:
        pass
    db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    await releasemaphore(chat=chat)
async def generate_image_wrapper(update, context, _message=None, chat=None, lang=None):
    if not chat:
        chat = await chat_check(update, context)
    if not lang:
        lang = await lang_check(update, context, chat)
    if await is_previous_message_not_answered_yet(chat, lang, update): return
    task = bb(generate_image_handle(chat, lang, update, context, _message))
    bcs(handle_chat_task(chat, lang, task, update))

async def ask_timeout_handle(chat, lang, update: Update, context: CallbackContext, _message):
    keyboard = [[
        InlineKeyboardButton("✅", callback_data=f"new_dialog|true"),
        InlineKeyboardButton("❎", callback_data=f"new_dialog|false"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    new_dialog_message = {"user": _message, "date": datetime.now()}
    await add_dialog_message(chat, new_dialog_message)

    await update.effective_chat.send_message(f'{config.lang["mensajes"]["timeout_ask"][lang]}', reply_markup=reply_markup)
async def answer_timeout_handle(update: Update, context: CallbackContext):
    chat = await chat_check(update, context)
    lang = await lang_check(update, context, chat)
    query = update.callback_query
    await query.answer()
    new_dialog = query.data.split("|")[1]
    dialog_messages = db.get_dialog_messages(chat.id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.effective_chat.send_message(f'{config.lang["mensajes"]["timeout_nodialog"][lang]}')
        await releasemaphore(chat=chat)
        await new_dialog_handle(update, context, chat, lang)
        return
    elif 'bot' in dialog_messages[-1]: # already answered, do nothing
        await releasemaphore(chat=chat)
        return
    await query.message.delete()
    if new_dialog == "true":
        last_dialog_message = dialog_messages.pop()
        await releasemaphore(chat=chat)
        await new_dialog_handle(update, context, chat, lang)
        await message_handle(chat, lang, update, context, _message=last_dialog_message["user"])
    else:
        await releasemaphore(chat=chat)
        await retry_handle(update, context, chat, lang)

async def cancel_handle(update: Update, context: CallbackContext):
    chat = await chat_check(update, context)
    lang = await lang_check(update, context, chat)
    semaphore = chat_locks.get(chat.id)
    if semaphore and semaphore.locked():
        await releasemaphore(chat)
        if chat.id in chat_tasks:
            task = chat_tasks[chat.id]
            task.cancel()
        db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    else:
        await update.message.reply_text(f'{config.lang["mensajes"]["nadacancelado"][lang]}', parse_mode=ParseMode.HTML)

async def get_menu(menu_type, update: Update, context: CallbackContext, chat):
    menu_type_dict = getattr(config, menu_type)
    api_antigua = db.get_chat_attribute(chat.id, 'current_api')
    current_lang = db.get_chat_attribute(chat.id, 'current_lang')
    if api_antigua not in apis_vivas:
        db.set_chat_attribute(chat.id, "current_api", apis_vivas[0])
        await update.effective_chat.send_message(f'{config.lang["errores"]["menu_api_no_disponible"][current_lang].format(api_antigua=api_antigua, api_nueva=config.api["info"][db.get_chat_attribute(chat.id, "current_api")]["name"])}')
        pass
    modelos_disponibles = config.api["info"][db.get_chat_attribute(chat.id, "current_api")]["available_model"]
    if db.get_chat_attribute(chat.id, 'current_model') not in modelos_disponibles:
        db.set_chat_attribute(chat.id, "current_model", modelos_disponibles[0])
        await update.effective_chat.send_message(f'{config.lang["errores"]["model_no_compatible"][current_lang].format(api_actual_name=config.api["info"][db.get_chat_attribute(chat.id, "current_api")]["name"], new_model_name=config.model["info"][db.get_chat_attribute(chat.id, "current_model")]["name"])}')
        pass
    if menu_type == "model":
        item_keys = modelos_disponibles
    elif menu_type == "api":
        item_keys = apis_vivas
    else:
        item_keys = menu_type_dict[f"available_{menu_type}"]
    current_key = db.get_chat_attribute(chat.id, f"current_{menu_type}")
    if menu_type == "chat_mode":
        option_name = menu_type_dict["info"][current_key]["name"][current_lang]
    elif menu_type == "lang":
        option_name = menu_type_dict["info"]["name"][current_lang]
    else:
        option_name = menu_type_dict["info"][current_key]["name"]
    text = f"<b>{config.lang['info']['actual'][current_lang]}</b>\n\n{str(option_name)}, {config.lang['info']['description'][current_lang] if menu_type == 'lang' else menu_type_dict['info'][current_key]['description'][current_lang]}\n\n<b>{config.lang['info']['seleccion'][current_lang]}</b>:"
    num_cols = 2
    import math
    num_rows = math.ceil(len(item_keys) / num_cols)
    options = [
        [
        menu_type_dict["info"][current_key]["name"][current_lang] if menu_type == "chat_mode" else (config.lang['info']['name'][current_key] if menu_type == 'lang' else menu_type_dict["info"][current_key]["name"]),
        f"set_{menu_type}|{current_key}",
        current_key,
        ]
        for current_key in item_keys
    ]
    reply_markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(name, callback_data=data) 
                for name, data, selected in options[i::num_rows]
            ]
            for i in range(num_rows)
        ]
    )
    return text, reply_markup

async def chat_mode_handle(update: Update, context: CallbackContext):
    chat = await chat_check(update, context)
    text, reply_markup = await get_menu(menu_type="chat_mode", update=update, context=context, chat=chat)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def chat_mode_callback_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    text, reply_markup = await get_menu(menu_type="chat_mode", update=update, context=context)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

async def set_chat_mode_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    chat = await chat_check(update, context)
    lang = await lang_check(update, context, chat)
    await query.answer()
    mode = query.data.split("|")[1]
    db.set_chat_attribute(chat.id, "current_chat_mode", mode)
    text, reply_markup = await get_menu(menu_type="chat_mode", update=update, context=context, chat=chat)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        await update.effective_chat.send_message(f"{config.chat_mode['info'][db.get_chat_attribute(chat.id, 'current_chat_mode')]['welcome_message'][lang]}", parse_mode=ParseMode.HTML)
        db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

async def model_handle(update: Update, context: CallbackContext):
    chat = await chat_check(update, context)
    text, reply_markup = await get_menu(menu_type="model", update=update, context=context,chat=chat)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def model_callback_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    text, reply_markup = await get_menu(menu_type="model", update=update, context=context)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

async def set_model_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    chat = await chat_check(update, context)
    await query.answer()
    _, model = query.data.split("|")
    db.set_chat_attribute(chat.id, "current_model", model)
    text, reply_markup = await get_menu(menu_type="model", update=update, context=context, chat=chat)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

async def api_handle(update: Update, context: CallbackContext):
    chat = await chat_check(update, context)
    text, reply_markup = await get_menu(menu_type="api", update=update, context=context, chat=chat)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def api_callback_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    text, reply_markup = await get_menu(menu_type="api", update=update, context=context)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

async def set_api_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    chat = await chat_check(update, context)
    await query.answer()
    _, api = query.data.split("|")
    db.set_chat_attribute(chat.id, "current_api", api)
    text, reply_markup = await get_menu(menu_type="api", update=update, context=context, chat=chat)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg=f'{config.lang["errores"]["handler_excepcion"][config.pred_lang]}:', exc_info=context.error)
    try:
        # collect error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f'{config.lang["errores"]["handler_excepcion"][config.pred_lang]}:\n'
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # # split text into multiple messages due to 4096 character limit
        # for message_chunk in split_text_into_chunks(message, 4096):
        #     try:
        #         await context.bot.send_message(update.effective_chat_id, message_chunk, parse_mode=ParseMode.HTML)
        #     except telegram.error.BadRequest:
        #         # answer has invalid characters, so we send it without parse_mode
        #         await context.bot.send_message(update.effective_chat_id, message_chunk)
    except:
        await context.bot.send_message(f'{config.lang["errores"]["handler_error_handler"][config.pred_lang]}')

async def post_init(application: Application):
    bb(ejecutar_obtener_vivas())
    commandos = [
        BotCommand("/new", "🌟"),
        BotCommand("/chat_mode", "💬"),
        BotCommand("/retry", "🔄"),
        BotCommand("/model", "🧠"),
        BotCommand("/api", "🔌"),
        BotCommand("/img", "🖼️"),
        BotCommand("/lang", "🌍"),
        BotCommand("/help", "ℹ️")
    ]
    await application.bot.set_my_commands(commandos)

async def lang_handle(update: Update, context: CallbackContext):
    chat = await chat_check(update, context)
    text, reply_markup = await get_menu(menu_type="lang", update=update, context=context, chat=chat)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def lang_callback_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    text, reply_markup = await get_menu(menu_type="lang", update=update, context=context)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

async def set_lang_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    chat = await chat_check(update, context)
    await query.answer()
    _, lang = query.data.split("|")
    await cambiar_idioma(update, context, chat, lang)
    text, reply_markup = await get_menu(menu_type="lang", update=update, context=context, chat=chat)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        db.set_chat_attribute(chat.id, "last_interaction", datetime.now())
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

async def ejecutar_obtener_vivas():
    while True:
        try:
            await obtener_vivas()
        except asyncio.CancelledError:
            break
        await sleep(60 * config.apicheck_minutes)  # Cada 60 segundos * 60 minutos

def run_bot() -> None:
    try:
        application = (
            ApplicationBuilder()
            .token(config.telegram_token)
            .concurrent_updates(True)
            .rate_limiter(AIORateLimiter(max_retries=8))
            .post_init(post_init)
            .build()
        )
        # add handlers
        if config.user_whitelist:
            usernames = []
            user_ids = []
            for user in config.user_whitelist:
                user = user.strip()
                if user.isnumeric():
                    user_ids.append(int(user))
                else:
                    usernames.append(user)
            user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids)
        else:
            user_filter = filters.ALL
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle_wrapper))
        application.add_handler(MessageHandler(filters.AUDIO & user_filter, transcribe_message_wrapper))
        application.add_handler(MessageHandler(filters.VOICE & user_filter, transcribe_message_wrapper))
        application.add_handler(MessageHandler(filters.PHOTO & user_filter, ocr_image_wrapper))
        docfilter = (filters.Document.FileExtension("pdf") | filters.Document.FileExtension("lrc"))
        application.add_handler(MessageHandler(docfilter & user_filter, document_wrapper))
        application.add_handler(MessageHandler(filters.Document.Category('text/') & user_filter, document_wrapper))
        
        application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
        application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
        application.add_handler(CommandHandler("help_group_chat", help_group_chat_handle, filters=user_filter))
        application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
        application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
        application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))
        application.add_handler(CommandHandler("chat_mode", chat_mode_handle, filters=user_filter))
        application.add_handler(CommandHandler("model", model_handle, filters=user_filter))
        application.add_handler(CommandHandler("api", api_handle, filters=user_filter))
        application.add_handler(CommandHandler("img", generate_image_wrapper, filters=user_filter))
        application.add_handler(CommandHandler("lang", lang_handle, filters=user_filter))
        application.add_handler(CallbackQueryHandler(set_lang_handle, pattern="^set_lang"))

        application.add_handler(CallbackQueryHandler(answer_timeout_handle, pattern="^new_dialog"))
        application.add_handler(CallbackQueryHandler(chat_mode_callback_handle, pattern="^get_menu"))
        application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))
        application.add_handler(CallbackQueryHandler(model_callback_handle, pattern="^get_menu"))
        application.add_handler(CallbackQueryHandler(set_model_handle, pattern="^set_model"))
        application.add_handler(CallbackQueryHandler(api_callback_handle, pattern="^get_menu"))
        application.add_handler(CallbackQueryHandler(set_api_handle, pattern="^set_api"))

        application.add_error_handler(error_handle)
        application.run_polling()
    except Exception as e:
        logger.error(f'{config.lang["errores"]["error"][config.pred_lang]}: {e}.')

if __name__ == "__main__":
    run_bot()
