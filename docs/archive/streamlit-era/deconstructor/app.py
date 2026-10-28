import os
import tempfile
import uuid
import streamlit as st
from datetime import datetime

from langchain_community.vectorstores import Chroma
from shared.config import CHROMA_PERSIST_DIR
from shared.embeddings import get_embeddings
from deconstructor.ingestion import ingest_pdfs
from deconstructor.retriever import retrieve
from deconstructor.memory import build_memory
from deconstructor.database import init_db, SessionLocal, ChatSession, ChatMessage
from deconstructor.llm import ask

@st.cache_resource
def _init_db_once():
    init_db()
    return True

_init_db_once()

st.set_page_config(page_title="Research Paper Deconstructor", layout="wide")
st.title("Research Paper Deconstructor")

@st.cache_resource
def init_chroma():
    return Chroma(
        collection_name="deconstructor",
        persist_directory=str(CHROMA_PERSIST_DIR),
        embedding_function=get_embeddings(),
    )

@st.cache_data(ttl=60)
def get_all_sessions():
    db = SessionLocal()
    sessions = db.query(ChatSession).order_by(ChatSession.last_active.desc()).all()
    db.close()
    return [(s.id, s.name) for s in sessions]

chroma = init_chroma()

if "session_id" not in st.session_state:
    st.session_state.session_id = None
if "memory" not in st.session_state:
    st.session_state.memory = None

with st.sidebar:
    if st.button("New Chat"):
        sid = str(uuid.uuid4())
        db = SessionLocal()
        db.add(ChatSession(id=sid, name="Untitled"))
        db.commit()
        db.close()
        st.session_state.session_id = sid
        st.session_state.memory = build_memory([])
        get_all_sessions.clear()
        st.rerun()

    sessions = get_all_sessions()

    for sid, sname in sessions:
        if st.button(sname or sid, key=sid):
            db = SessionLocal()
            msgs = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_id == sid)
                .order_by(ChatMessage.created_at)
                .all()
            )
            db.close()
            st.session_state.session_id = sid
            st.session_state.memory = build_memory(
                [{"role": m.role, "content": m.content} for m in msgs]
            )
            st.rerun()

if not st.session_state.session_id:
    st.info("Create or select a chat.")
    st.stop()

if "_cached_msgs" not in st.session_state or st.session_state.get("_cached_session") != st.session_state.session_id:
    db = SessionLocal()
    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == st.session_state.session_id)
        .order_by(ChatMessage.created_at)
        .all()
    )
    db.close()
    st.session_state._cached_msgs = msgs
    st.session_state._cached_session = st.session_state.session_id
else:
    msgs = st.session_state._cached_msgs

for m in msgs:
    with st.chat_message("user" if m.role == "human" else "assistant"):
        st.markdown(m.content)

with st.expander("Upload PDFs", expanded=False):
    uploaded = st.file_uploader("Upload PDFs", type="pdf", accept_multiple_files=True)
    if uploaded and st.button("Process"):
        files = []
        for f in uploaded:
            path = os.path.join(tempfile.gettempdir(), f.name)
            with open(path, "wb") as out:
                out.write(f.getvalue())
            files.append({"path": path, "filename": f.name, "source": "upload"})
        ingest_pdfs(chroma, st.session_state.session_id, files)
        st.success("Processed")
        st.rerun()

q = st.chat_input("Ask about the document")
if q:
    with st.chat_message("user"):
        st.markdown(q)

    ctx_docs = retrieve(chroma, st.session_state.session_id, q)
    context = "\n\n".join(d.page_content for d in ctx_docs)

    answer = ask(f"Context:\n{context}\n\nQuestion:\n{q}\nAnswer:")
    with st.chat_message("assistant"):
        st.markdown(answer)

    db = SessionLocal()
    db.add(ChatMessage(session_id=st.session_state.session_id, role="human", content=q))
    db.add(ChatMessage(session_id=st.session_state.session_id, role="assistant", content=answer))
    s = db.query(ChatSession).get(st.session_state.session_id)
    if s:
        s.last_active = datetime.utcnow()
    db.commit()
    db.close()
