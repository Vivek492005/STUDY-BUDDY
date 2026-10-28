def retrieve(vectorstore, session_id: str, question: str):
    """Retrieve relevant documents from vectorstore."""
    docs = vectorstore.similarity_search(question, k=5)
    return docs

