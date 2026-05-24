import os
import json
import sqlite3
import asyncio
import random
import tempfile
import chromadb
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from openai import OpenAI, AsyncOpenAI
from elevenlabs.client import ElevenLabs
from apscheduler.schedulers.background import BackgroundScheduler
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()

DATA_DIR = "memory_data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "chats_final_v2.db")

# Using Groq's insanely fast Llama 3 endpoint
LLM_MODEL = 'llama-3.1-8b-instant'
MY_CHAT_ID = os.getenv("MY_TELEGRAM_CHAT_ID")
IST = ZoneInfo("Asia/Kolkata")

firebase_json_string = os.getenv("FIREBASE_CREDENTIALS_JSON")
db_firestore = None
if firebase_json_string:
    try:
        cred = credentials.Certificate(json.loads(firebase_json_string))
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db_firestore = firestore.client()
    except Exception as e:
        print(f"Firebase Init Error: {e}")

# We use the OpenAI library but point it at Groq's servers
groq_api_key = os.getenv("GROQ_API_KEY")
async_ai_client = AsyncOpenAI(api_key=groq_api_key, base_url="https://api.groq.com/openai/v1")
sync_ai_client = OpenAI(api_key=groq_api_key, base_url="https://api.groq.com/openai/v1")

eleven_key = os.getenv("ELEVENLABS_API_KEY")
voice_client = ElevenLabs(api_key=eleven_key) if eleven_key else None
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")

chroma_client = chromadb.PersistentClient(path=f"{DATA_DIR}/vector_space")
vector_memory = chroma_client.get_or_create_collection(name="relationship_history")
tg_application = None

def init_sqlite():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, 
            role TEXT, 
            content TEXT, 
            synced_to_cloud INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def save_to_sqlite(chat_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)", (str(chat_id), role, content))
    conn.commit()
    conn.close()

def get_short_term_history(chat_id, limit=20):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT role, content FROM (
            SELECT role, content, id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?
        ) ORDER BY id ASC
    ''', (str(chat_id), limit))
    rows = cursor.fetchall()
    conn.close()
    
    formatted_history = []
    for role, content in rows:
        # OpenAI/Groq API requires roles to be 'user', 'assistant', or 'system'
        api_role = "assistant" if role == "model" else role
        formatted_history.append({"role": api_role, "content": content})
    return formatted_history

async def generate_memory_search_query(chat_id, user_text):
    # Optimization to save limits on short texts
    if len(user_text.split()) <= 4:
        return user_text

    prompt = f"""
    Analyze the user's latest message: "{user_text}"
    If they refer to a past event, joke, or person, extract the core keywords needed to look up that memory.
    Examples:
    - "remember what you said back then?" -> "promises secrets past conversations"
    - "i am passing by our favorite spot" -> "favorite cafe restaurant meeting spot dating"
    Output ONLY the search keywords or phrases. No explanation. If purely generic, output the user's text exactly.
    """
    try:
        response = await async_ai_client.chat.completions.create(
            model=LLM_MODEL, 
            messages=[{"role": "user", "content": prompt}],
            max_tokens=20
        )
        query = response.choices[0].message.content.strip()
        return query if query else user_text
    except Exception:
        return user_text

def fetch_relevant_memories(query_text, count=4):
    if vector_memory.count() == 0:
        return "No historical memories loaded yet."
    results = vector_memory.query(query_texts=[query_text], n_results=count)
    if not results['documents']: return "No relevant past memories found."
    
    memory_strings = []
    for sublist in results['documents']:
        for doc in sublist:
            memory_strings.append(f"- {doc}")
    return "\n".join(memory_strings)

def save_to_vector_space(msg_id, text, speaker):
    vector_memory.add(documents=[f"[{speaker}]: {text}"], ids=[f"live_{msg_id}_{random.randint(1000,999999)}"])

async def send_voice_note(context, chat_id, text_to_speak):
    if not voice_client:
        await context.bot.send_message(chat_id=chat_id, text=text_to_speak)
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
    record_time = min(len(text_to_speak) / 10.0, 10.0)
    await asyncio.sleep(record_time)
    
    try:
        audio_generator = voice_client.generate(
            text=text_to_speak,
            voice=ELEVENLABS_VOICE_ID,
            model="eleven_multilingual_v2"
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            for chunk in audio_generator:
                if chunk: f.write(chunk)
            temp_path = f.name
            
        with open(temp_path, 'rb') as audio_file:
            await context.bot.send_voice(chat_id=chat_id, voice=audio_file)
        os.remove(temp_path)
    except Exception as e:
        print(f"ElevenLabs Error: {e}")
        await context.bot.send_message(chat_id=chat_id, text=text_to_speak.lower())

def check_and_send_spontaneous_message():
    global tg_application
    if not tg_application or not MY_CHAT_ID: return
    
    current_hour = datetime.now(IST).hour
    if 1 <= current_hour <= 7 or random.random() > 0.25: 
        return 

    current_time_str = datetime.now(IST).strftime("%A, %B %d, %Y at %I:%M %p")
    instruction = f"""
    You are Sanskruti Jadhav texting your boyfriend Aditya out of nowhere. 
    Current Time: {current_time_str}. 
    RULES:
    1. Send ONE short, random text.
    2. Lowercase only, no ending punctuation.
    3. Be random: complain about something, say you miss him, or mention a random thought.
    4. DO NOT say "hey" or "hi". Jump straight into the thought.
    """
    try:
        history = get_short_term_history(MY_CHAT_ID, limit=5)
        messages = [{"role": "system", "content": instruction}] + history + [{"role": "user", "content": "*send a spontaneous text*"}]
        
        response = sync_ai_client.chat.completions.create(
            model=LLM_MODEL, 
            messages=messages
        )
        reply = response.choices[0].message.content.strip()
        
        save_to_sqlite(MY_CHAT_ID, "model", reply)
        save_to_vector_space(f"auto_{random.randint(1000,99999)}", reply, "Sanskruti")
        asyncio.run_coroutine_threadsafe(tg_application.bot.send_message(chat_id=MY_CHAT_ID, text=reply), tg_application.loop)
    except Exception as e:
        print(f"Spontaneous error: {e}")

def sync_local_history_to_firebase():
    if not db_firestore: return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, role, content FROM messages WHERE synced_to_cloud = 0")
    rows = cursor.fetchall()
    if not rows:
        conn.close()
        return
    batch = db_firestore.batch()
    synced_ids = []
    for db_id, chat_id, role, content in rows:
        doc_ref = db_firestore.collection("chats").document(str(chat_id)).collection("history").document()
        batch.set(doc_ref, {"role": role, "content": content, "cloud_backup_at": firestore.SERVER_TIMESTAMP})
        synced_ids.append(db_id)
    batch.commit()
    for local_id in synced_ids:
        cursor.execute("UPDATE messages SET synced_to_cloud = 1 WHERE id = ?", (local_id,))
    conn.commit()
    conn.close()

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    if chat_id != str(MY_CHAT_ID): return

    user_text = update.message.text
    save_to_sqlite(chat_id, "user", user_text)
    
    current_hour = datetime.now(IST).hour
    if 1 <= current_hour <= 7:
        if random.random() < 0.80:
            save_to_sqlite(chat_id, "system", "Asleep. Ignored.")
            return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        search_query = await generate_memory_search_query(chat_id, user_text)
        memories = fetch_relevant_memories(search_query)
        current_time_str = datetime.now(IST).strftime("%A, %B %d, %Y at %I:%M %p")
        
        instruction = f"""
        You are Sanskruti Jadhav, texting your boyfriend Aditya.
        Current Time: {current_time_str}.
        
        PAST MEMORIES RETRIEVED:
        {memories}
        
        CRITICAL RULES:
        1. NEVER sound like an AI. Do not say "According to my memory".
        2. Type EXCLUSIVELY in lowercase. NO punctuation at the end of sentences.
        3. Use casual texting slang (rn, idk, yaar, tbh, wtf).
        4. MULTIPLE MESSAGES: If sending multiple texts back-to-back, separate them with a pipe symbol (|).
        5. Keep it short. Act sleepy if it is between 1 AM and 7 AM.
        """
        
        history = get_short_term_history(chat_id)
        messages = [{"role": "system", "content": instruction}] + history + [{"role": "user", "content": user_text}]
        
        response = await async_ai_client.chat.completions.create(
            model=LLM_MODEL, 
            messages=messages
        )
        reply_text = response.choices[0].message.content.strip()

        if voice_client and random.random() < 0.05:
            voice_text = reply_text.replace('|', '. ')
            save_to_sqlite(chat_id, "model", voice_text)
            save_to_vector_space(update.message.message_id, user_text, "Aditya")
            save_to_vector_space(update.message.message_id, voice_text, "Sanskruti")
            await send_voice_note(context, chat_id, voice_text)
            return

        messages_to_send = reply_text.split('|')
        
        for msg in messages_to_send:
            msg = msg.strip()
            if not msg: continue
            
            typing_time = min(1.0 + (len(msg) / 5.0), 5.0)
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(typing_time)
            
            await update.message.reply_text(msg)
            save_to_sqlite(chat_id, "model", msg)
            save_to_vector_space(update.message.message_id, msg, "Sanskruti")

        save_to_vector_space(update.message.message_id, user_text, "Aditya")

    except Exception as e:
        print(f"Error handling chat: {e}")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(2)
        await update.message.reply_text("give me a sec lol my phone is lagging")

if __name__ == '__main__':
    init_sqlite()
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(sync_local_history_to_firebase, 'interval', days=1)
    scheduler.add_job(check_and_send_spontaneous_message, 'interval', minutes=90)
    scheduler.start()
    
    tg_application = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    tg_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
    
    print("Worker engine active. Sanskruti is online 24/7...")
    tg_application.run_polling()
    
