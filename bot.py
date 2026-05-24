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

# --- Configuration & File Setup ---
# Renamed directory and DB to permanently bypass old ghost files
DATA_DIR = "memory_data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "chats_final_v1.db")

GEMINI_MODEL = 'gemini-2.5-pro'
MY_CHAT_ID = os.getenv("MY_TELEGRAM_CHAT_ID")

# --- Firebase Initialization ---
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

# --- API Clients ---
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
eleven_key = os.getenv("ELEVENLABS_API_KEY")
voice_client = ElevenLabs(api_key=eleven_key) if eleven_key else None
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

chroma_client = chromadb.PersistentClient(path=f"{DATA_DIR}/vector_space")
vector_memory = chroma_client.get_or_create_collection(name="relationship_history")

tg_application = None

# --- SQLite Database (Short-Term Memory) ---
def init_sqlite():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, 
            role TEXT, 
            content TEXT, 
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
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
            SELECT role, content FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?
        ) ORDER BY timestamp ASC
    ''', (str(chat_id), limit))
    rows = cursor.fetchall()
    conn.close()
    
    history = []
    for role, content in rows:
        history.append(types.Content(role=role, parts=[types.Part.from_text(content)]))
    return history

# --- Vector Database (Long-Term Memory) ---
def fetch_relevant_memories(query_text, count=4):
    if vector_memory.count() == 0:
        return "No historical memories loaded yet."
    results = vector_memory.query(query_texts=[query_text], n_results=count)
    if not results['documents']: 
        return ""
    return "\n".join([doc for sublist in results['documents'] for doc in sublist])

def save_to_vector_space(msg_id, text, speaker):
    vector_memory.add(
        documents=[f"[{speaker}]: {text}"],
        ids=[f"live_{msg_id}_{random.randint(1000,999999)}"]
    )

# --- Automation Tasks ---
def check_and_send_spontaneous_message():
    global tg_application
    if not tg_application or not MY_CHAT_ID: return
    if random.random() > 0.25: return # 25% chance to run every cycle

    current_time_str = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    instruction = f"You are roleplaying as Sanskruti Jadhav texting your boyfriend Aditya. Current Time: {current_time_str}. Send a random, short, spontaneous text. Keep it lowercase and casual."
    
    try:
        history = get_short_term_history(MY_CHAT_ID, limit=15)
        chat_session = ai_client.chats.create(model=GEMINI_MODEL, history=history, config={'system_instruction': instruction})
        reply = chat_session.send_message("Initiate a random text conversation with Aditya.").text
        
        save_to_sqlite(MY_CHAT_ID, "model", reply)
        save_to_vector_space(f"auto_{random.randint(1000,99999)}", reply, "Sanskruti")
        
        asyncio.run_coroutine_threadsafe(tg_application.bot.send_message(chat_id=MY_CHAT_ID, text=reply), tg_application.loop)
    except Exception as e:
        print(f"Automation Error: {e}")

def sync_local_history_to_firebase():
    if not db_firestore: return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, role, content, timestamp FROM messages WHERE synced_to_cloud = 0")
    rows = cursor.fetchall()
    if not rows:
        conn.close()
        return

    batch = db_firestore.batch()
    synced_ids = []
    for db_id, chat_id, role, content, timestamp in rows:
        doc_ref = db_firestore.collection("chats").document(str(chat_id)).collection("history").document()
        batch.set(doc_ref, {"role": role, "content": content, "local_timestamp": timestamp, "cloud_backup_at": firestore.SERVER_TIMESTAMP})
        synced_ids.append(db_id)

    batch.commit()
    for local_id in synced_ids:
        cursor.execute("UPDATE messages SET synced_to_cloud = 1 WHERE id = ?", (local_id,))
    conn.commit()
    conn.close()

# --- Core Chat Handler ---
async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    if chat_id != str(MY_CHAT_ID): return

    user_text = update.message.text
    save_to_sqlite(chat_id, "user", user_text)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    memories = fetch_relevant_memories(user_text)
    current_time_str = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    
    instruction = f"You are Sanskruti Jadhav, texting your boyfriend Aditya. Current Time: {current_time_str}. RELEVANT MEMORIES: {memories}. Respond naturally, in lowercase, with short bursts."
    
    chat_session = ai_client.chats.create(model=GEMINI_MODEL, history=get_short_term_history(chat_id), config={'system_instruction': instruction})
    reply_text = chat_session.send_message(user_text).text

    save_to_sqlite(chat_id, "model", reply_text)
    await update.message.reply_text(reply_text)
    
    save_to_vector_space(update.message.message_id, user_text, "Aditya")
    save_to_vector_space(update.message.message_id, reply_text, "Sanskruti")

if __name__ == '__main__':
    init_sqlite()
    scheduler = BackgroundScheduler()
    scheduler.add_job(sync_local_history_to_firebase, 'interval', days=1)
    scheduler.add_job(check_and_send_spontaneous_message, 'interval', minutes=90)
    scheduler.start()
    
    tg_application = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    tg_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
    
    print("Worker engine active. Sanskruti is online 24/7...")
    tg_application.run_polling()
    
