import os
import json
import chromadb

# Connect to the exact same vector database as bot.py
DATA_DIR = "memory_data"
os.makedirs(DATA_DIR, exist_ok=True)
chroma_client = chromadb.PersistentClient(path=f"{DATA_DIR}/vector_space")
vector_memory = chroma_client.get_or_create_collection(name="relationship_history")

def fix_insta_encoding(text):
    """Instagram JSON exports have broken UTF-8 encoding. This fixes emojis and apostrophes."""
    try:
        return text.encode('latin1').decode('utf-8')
    except:
        return text

def load_messages():
    if not os.path.exists("message_1.json"):
        print("Error: message_1.json not found in the current directory!")
        return

    with open("message_1.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    # Instagram JSONs are usually newest-first. Reverse to process chronologically.
    messages = data.get("messages", [])
    messages.reverse() 

    documents = []
    ids = []
    count = 0

    print(f"Found {len(messages)} messages. Processing...")

    for msg in messages:
        if "content" in msg and "sender_name" in msg:
            raw_text = msg["content"]
            raw_sender = msg["sender_name"]

            # Fix the text encoding
            clean_text = fix_insta_encoding(raw_text)

            # Assign Speaker (Matches your name in the Instagram JSON)
            speaker = "Aditya" if "Aditya" in raw_sender else "Sanskruti"

            # Format it exactly how bot.py expects to read it
            doc_string = f"[{speaker}]: {clean_text}"
            
            documents.append(doc_string)
            ids.append(f"historical_chat_{count}")
            count += 1

    if count == 0:
        print("No valid text messages found. Check the JSON format.")
        return

    # Insert into ChromaDB in chunks to avoid overloading the memory
    batch_size = 500
    for i in range(0, len(documents), batch_size):
        batch_docs = documents[i:i+batch_size]
        batch_ids = ids[i:i+batch_size]
        
        vector_memory.add(documents=batch_docs, ids=batch_ids)
        print(f"Loaded messages {i} to {i + len(batch_docs)} into vector memory...")

    print(f"\nSuccess! {count} permanent memories injected. You can start bot.py now.")

if __name__ == '__main__':
    load_messages()
