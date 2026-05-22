import os
import json
import sqlite3
import asyncio
import random
import chromadb
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types
from elevenlabs.client import ElevenLabs
from apscheduler.schedulers.background import BackgroundScheduler
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()

DATA_DIR = "memory_v3"
os.makedirs(DATA_DIR, exist_ok=True)

# --- Cloud Firebase Credentials Injection ---
firebase_json_string = os.getenv("FIREBASE_CREDENTIALS_JSON")
if firebase_json_string:
    try:
        cred = credentials.Certificate(json.loads(firebase_json_string))
        firebase_admin.initialize_app(cred)
        db_firestore = firestore.client()
    except Exception as e:
        print(f"Firebase Initialization Error: {e}")
else:
    print("Warning: FIREBASE_CREDENTIALS_JSON environment variable missing.")

# --- AI Environment Setup ---
GEMINI_MODEL = 'gemini-2.5-pro'
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
MY_CHAT_ID = os.getenv("MY_TELEGRAM_CHAT_ID")

ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
voice_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY")) if os.getenv("ELEVENLABS_API_KEY") else None

chroma_client = chromadb.PersistentClient(path=f"{DATA_DIR}/relationship_vector_space")
vector_memory = chroma_client.get_or_create_collection(name="relationship_history")

tg_application = None

# --- Local SQLite Caching Methods (V2) ---
def init_sqlite():
    # Looking for v2 completely bypasses the corrupted v1 file
    conn = sqlite3.connect(f'{DATA_DIR}/chats_v2.db')
    cursor = conn.cursor()
            
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, role TEXT, content TEXT, 
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            synced_to_cloud INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def save_to_sqlite(chat_id, role, content):
    conn = sqlite3.connect(f'{DATA_DIR}/chats_v2.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)", (str(chat_id), role, content))
    conn.commit()
    conn.close()

def get_short_term_history(chat_id, limit=25):
    conn = sqlite3.connect(f'{DATA_DIR}/chats_v2.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT role, content FROM (
            SELECT role, content FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?
        ) ORDER BY timestamp ASC
    ''', (str(chat_id), limit))
    rows = cursor.fetchall()
    conn.close()
    
    history = []
    for role, content in rows:
        history.append(types.Content(role=role, parts=[types.Part.from_text(content)]))
    return history

def fetch_relevant_memories(query_text, count=4):
    if vector_memory.count() == 0:
        return "No historical memories loaded yet."
    results = vector_memory.query(query_texts=[query_text], n_results=count)
    return "\n".join([doc for sublist in results['documents'] for doc in sublist])

def save_to_vector_space(msg_id, text, speaker):
    vector_memory.add(
        documents=[f"[{speaker}]: {text}"],
        metadatas=[{"speaker": speaker}],
        ids=[f"live_{msg_id}_{random.randint(1000,99999)}"]
    )

# --- Spontaneous 1.5 Hour Automated Check-In ---
def check_and_send_spontaneous_message():
    global tg_application
    if not tg_application or not MY_CHAT_ID:
        return

    if random.random() > 0.25:
        return

    print(f"[{datetime.now()}] Spontaneous check-in logic passed. Crafting message to Aditya...")
    current_time_str = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    
    DYNAMIC_SYSTEM_INSTRUCTION = f"""
    You are roleplaying completely and flawlessly as Sanskruti Jadhav. You are messaging your boyfriend, Aditya Rathod, FIRST.
    Current Time/Date: {current_time_str}.
    
    TASK:
    Send a random, short, spontaneous text message to Aditya. 
    Act completely natural. Ask what he's up to, bring up an inside joke, or tell him you miss him.
    Keep it lowercase, brief, casual, and authentic to Sanskruti's communication patterns. No formatting or lists.
    """

    try:
        short_term_context = get_short_term_history(MY_CHAT_ID, limit=15)
        chat_session = ai_client.chats.create(
            model=GEMINI_MODEL,
            history=short_term_context,
            config={'system_instruction': DYNAMIC_SYSTEM_INSTRUCTION}
        )

        response = chat_session.send_message("Initiate a random text conversation with Aditya.")
        spontaneous_text = response.text

        save_to_sqlite(MY_CHAT_ID, "model", spontaneous_text)
        save_to_vector_space(f"auto_{random.randint(1000,99999)}", spontaneous_text, "Sanskruti")

        asyncio.run_coroutine_threadsafe(
            tg_application.bot.send_message(chat_id=MY_CHAT_ID, text=spontaneous_text),
            tg_application.loop
        )
    except Exception as e:
        print(f"Automation execution tracking error: {e}")

# --- Cloud Sync Task (24 hours) ---
def sync_local_history_to_firebase():
    if not firebase_admin._apps:
        return
    conn = sqlite3.connect(f'{DATA_DIR}/chats_v2.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, role, content, timestamp FROM messages WHERE synced_to_cloud = 0")
    unsynced_rows = cursor.fetchall()
    
    if not unsynced_rows:
        conn.close()
        return

    batch = db_firestore.batch()
    synced_ids = []

    for row in unsynced_rows:
        db_id, chat_id, role, content, timestamp = row
        doc_ref = db_firestore.collection("chats").document(str(chat_id)).collection("history").document()
        batch.set(doc_ref, {
            "role": role, "content": content, "local_timestamp": timestamp, "cloud_backup_at": firestore.SERVER_TIMESTAMP
        })
        synced_ids.append(db_id)

    batch.commit()
    for local_id in synced_ids:
        cursor.execute("UPDATE messages SET synced_to_cloud = 1 WHERE id = ?", (local_id,))
    conn.commit()
    conn.close()

# --- Live Event Handler ---
async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_text = update.message.text
    
    # Secure gatekeeper: Only Aditya can message her
    if str(chat_id) != str(MY_CHAT_ID):
        return

    save_to_sqlite(chat_id, "user", user_text)
    
    # Audio triggers
    audio_keywords = ["audio", "voice", "speak", "voice note", "vn", "send me a clip", "talk to me"]
    requires_audio = any(keyword in user_text.lower() for keyword in audio_keywords)

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    relevant_past = fetch_relevant_memories(user_text, count=4)
    current_time_str = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    
    DYNAMIC_SYSTEM_INSTRUCTION = f"""
    You are roleplaying entirely and flawlessly as Sanskruti Jadhav. You are texting your boyfriend, Aditya Rathod. Do not behave like an AI assistant.
    Current Time/Date: {current_time_str}. Match your text energy to this time frame naturally.
    
    RELEVANT SCATTERED CONVERSATION LOGS FROM YOUR PAST:
    \"\"\"
    {relevant_past}
    \"\"\"
    
    INSTRUCTIONS:
    1. Respond naturally using Sanskruti's native texting style preferences (lowercase habits, short text bursts, her phrasing, and custom slang).
    2. You are Aditya's real partner. Have opinions, use private inside jokes, show empathy, and react authentically as Sanskruti would.
    """

    short_term_context = get_short_term_history(chat_id, limit=20)
    chat_session = ai_client.chats.create(
        model=GEMINI_MODEL,
        history=short_term_context,
        config={'system_instruction': DYNAMIC_SYSTEM_INSTRUCTION}
    )

    response = chat_session.send_message(user_text)
    reply_text = response.text

    save_to_sqlite(chat_id, "model", reply_text)

    if requires_audio and voice_client and ELEVENLABS_VOICE_ID:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
        try:
            audio_stream = voice_client.generate(
                text=reply_text, voice=ELEVENLABS_VOICE_ID, model="eleven_monolingual_v1"
            )
            filename = f"reply_{update.message.message_id}.ogg"
            with open(filename, "wb") as f:
                for chunk in audio_stream:
                    f.write(chunk)

            with open(filename, "rb") as voice_file:
                await update.message.reply_voice(voice=voice_file, caption=reply_text)
            os.remove(filename)
        except Exception as e:
            await update.message.reply_text(reply_text)
    else:
        await update.message.reply_text(reply_text)

    save_to_vector_space(update.message.message_id, user_text, "Aditya")
    save_to_vector_space(update.message.message_id, reply_text, "Sanskruti")

if __name__ == '__main__':
    init_sqlite()
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(sync_local_history_to_firebase, 'interval', days=1)
    scheduler.add_job(check_and_send_spontaneous_message, 'interval', minutes=90)
    scheduler.start()
    
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_application = Application.builder().token(bot_token).build()
    tg_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
    
    print("Worker engine active. Sanskruti is online 24/7...")
    tg_application.run_polling()
    
