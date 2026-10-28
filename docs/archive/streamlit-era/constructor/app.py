import streamlit as st

st.set_page_config(page_title="IEEE Paper Constructor", layout="wide")

from constructor.github_loader import parse_repo, fetch_repo
from constructor.vectorstore import build_vectorstore
from constructor.analysis import analyze_repository
from constructor.paper_generator import generate_paper
from constructor.pdf_builder import build_pdf

st.title("IEEE Research Paper Constructor")

# ---------- Session state ----------
if "result" not in st.session_state:
    st.session_state.result = None
if "repo_name" not in st.session_state:
    st.session_state.repo_name = ""
if "author" not in st.session_state:
    st.session_state.author = ""
if "institution" not in st.session_state:
    st.session_state.institution = ""

# ---------- SHOW RESULT FIRST ----------
if st.session_state.result:
    sections = st.session_state.result

    st.subheader("Preview")
    st.markdown(f"### {sections.get('title','')}")
    st.markdown(sections.get("abstract", ""))

    pdf = build_pdf(
        sections,
        st.session_state.author,
        st.session_state.institution,
    )

    st.download_button(
        "Download PDF",
        pdf.getvalue(),
        file_name=f"{st.session_state.repo_name}_ieee_paper.pdf",
        mime="application/pdf",
    )

    st.divider()

# ---------- INPUT FORM ----------
with st.form("constructor_form", clear_on_submit=False):
    repo_url = st.text_input("GitHub Repository URL")
    author = st.text_input("Author Name")
    institution = st.text_input("Institution")
    submitted = st.form_submit_button(
        "Generate Paper",
        disabled=st.session_state.result is not None,
    )

# ---------- GENERATION ----------
if submitted and st.session_state.result is None:
    st.session_state.author = author
    st.session_state.institution = institution

    try:
        owner, repo = parse_repo(repo_url)
        if not owner:
            st.error("Invalid GitHub URL")
            st.stop()

        status = st.status("Processing", expanded=True)

        status.update(label="Fetching repository...")
        repo_data = fetch_repo(owner, repo, max_files=40)

        status.update(label="Building vector database...")
        vector_db = build_vectorstore(repo_data)

        status.update(label="Analyzing repository...")
        analysis = analyze_repository(repo_data)

        status.update(label="Generating paper...")
        sections = generate_paper(repo_data, analysis, vector_db)

        st.session_state.result = sections
        st.session_state.repo_name = repo

        status.update(label="Completed", state="complete")

        st.rerun()

    except Exception as e:
        st.error("Constructor failed")
        st.exception(e)
