import os
import json
import chromadb
from dotenv import load_dotenv

load_dotenv()
DATA_DIR = "data"

# The script will look for this exact file in your root folder
JSON_FILE_PATH = "message_1.json" 

def fix_encoding(text):
    """Fixes Meta's broken emoji and special character encoding."""
    try:
        return text.encode('latin1').decode('utf8')
    except:
        return text

def bootstrap_base_memory():
    os.makedirs(DATA_DIR, exist_ok=True)
    chroma_client = chromadb.PersistentClient(path=f"{DATA_DIR}/relationship_vector_space")
    vector_memory = chroma_client.get_or_create_collection(name="relationship_history")

    if vector_memory.count() > 0:
        print(f"Vector space already contains {vector_memory.count()} records. Skipping bootstrap.")
        return

    if not os.path.exists(JSON_FILE_PATH):
        print(f"Error: Could not find {JSON_FILE_PATH} in the directory.")
        return

    print("Parsing Instagram JSON logs directly into permanent vector space...")
    with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    messages = data.get('messages', [])
    messages.reverse() # Flip the array so the timeline goes oldest to newest
    
    documents, ids, metadatas = [], [], []
    valid_count = 0

    for msg in messages:
        if 'content' not in msg:
            continue # Skip image/reel attachments that don't have text
            
        raw_sender = msg.get('sender_name', '')
        sender_lower = fix_encoding(raw_sender).lower().strip()
        content = fix_encoding(msg.get('content', ''))
        content = content.replace('\n', ' ')
        
        # Matches exact IG handles AND display names to be 100% accurate
        if sender_lower == "_unknown_3622" or "aditya" in sender_lower or "rathod" in sender_lower:
            speaker_tag = "Aditya"
        elif sender_lower == "dark_choco2425" or "sanskruti" in sender_lower or "jadhav" in sender_lower:
            speaker_tag = "Sanskruti"
        else:
            continue
            
        documents.append(f"[{speaker_tag}]: {content}")
        metadatas.append({"speaker": speaker_tag})
        ids.append(f"hist_{valid_count}")
        valid_count += 1

        # Batch inserts to keep the database stable
        if len(documents) >= 100:
            vector_memory.add(documents=documents, metadatas=metadatas, ids=ids)
            documents, ids, metadatas = [], [], []

    # Insert any remaining messages
    if documents:
        vector_memory.add(documents=documents, metadatas=metadatas, ids=ids)
        
    print(f"Success! Memorized {valid_count} historical message blocks permanently.")

if __name__ == '__main__':
    bootstrap_base_memory()
    
