"""Feature 11-20 ultimate tool registry (second fragment).

Each entry maps a UI tool to a real function in studybuddy/. This file is
the metadata source; dispatch goes through the typed handlers in
labs_registry.py (Lab Phase 0). Params of type "textarea" with
"json": True are JSON-parsed before dispatch.
"""

TOOLS_B = [
    # ---- 11. Mock Interviews ----
    {
        "id": "mock/voice-prep",
        "feature": "Mock Interviews",
        "name": "Voice Interview Prep",
        "description": "Score a spoken answer's content and delivery pacing (WPM, fillers, tightness).",
        "module": "studybuddy.mock_interview",
        "function": "voice_interview_prep",
        "fields": [
            {"name": "question", "label": "Question (JSON, e.g. {\"question\": \"Tell me about yourself\"})", "type": "textarea", "required": True, "json": True},
            {"name": "answer_text", "label": "Your transcribed answer", "type": "textarea", "required": True},
            {"name": "seconds_taken", "label": "Seconds you spoke", "type": "number", "required": True},
        ],
    },
    {
        "id": "mock/company-intel",
        "feature": "Mock Interviews",
        "name": "Company Intel Card",
        "description": "See which question categories and roles a company actually asks about.",
        "module": "studybuddy.mock_interview",
        "function": "company_intel",
        "fields": [
            {"name": "company", "label": "Company name", "type": "text", "required": True},
        ],
    },
    {
        "id": "mock/redo-tracker",
        "feature": "Mock Interviews",
        "name": "Redo / Improvement Tracker",
        "description": "Track re-answers of the same questions and see your score trend.",
        "module": "studybuddy.mock_interview",
        "function": "redo_tracker",
        "fields": [
            {"name": "attempts", "label": "Attempts (JSON array of {question_id, score})", "type": "textarea", "required": True, "json": True},
        ],
    },

    # ---- 12. DSA Practice ----
    {
        "id": "dsa/pattern-mastery",
        "feature": "DSA Practice",
        "name": "Pattern Mastery Map",
        "description": "Mastery percentage per DSA pattern, with your weakest pattern flagged.",
        "module": "studybuddy.dsa_practice",
        "function": "pattern_mastery",
        "fields": [
            {"name": "solved_ids", "label": "Solved problem IDs (JSON array)", "type": "textarea", "required": True, "json": True},
            {"name": "attempted_ids", "label": "Attempted problem IDs (JSON array, optional)", "type": "textarea", "required": False, "json": True},
        ],
    },
    {
        "id": "dsa/mock-oa",
        "feature": "DSA Practice",
        "name": "Mock OA Simulator",
        "description": "Simulate a real online assessment: N questions, time limit, mixed difficulty.",
        "module": "studybuddy.dsa_practice",
        "function": "mock_oa",
        "fields": [
            {"name": "num_questions", "label": "Questions (1-4, default 2)", "type": "number", "required": False},
            {"name": "minutes", "label": "Time limit in minutes (10-180, default 60)", "type": "number", "required": False},
        ],
    },
    {
        "id": "dsa/spaced-repetition",
        "feature": "DSA Practice",
        "name": "Spaced Repetition Queue",
        "description": "Wrong problems resurface after 3/7/14 days so patterns stick.",
        "module": "studybuddy.dsa_practice",
        "function": "spaced_repetition_queue",
        "fields": [
            {"name": "mistakes", "label": "Mistakes (JSON array of {problem_id, wrong_at, streak})", "type": "textarea", "required": True, "json": True},
        ],
    },

    # ---- 13. Communication Coach ----
    {
        "id": "coach/video-presence",
        "feature": "Communication Coach",
        "name": "Video Presence Checklist",
        "description": "Self-scored checklist for AI video screens, with priority fixes.",
        "module": "studybuddy.communication_coach",
        "function": "video_presence_checklist",
        "fields": [
            {"name": "self_scores", "label": "Self scores (JSON dict of {check_id: true/false}, optional)", "type": "textarea", "required": False, "json": True},
        ],
    },
    {
        "id": "coach/hr-round",
        "feature": "Communication Coach",
        "name": "HR Round Simulator",
        "description": "Classic HR questions with frameworks and model answers to practice aloud.",
        "module": "studybuddy.communication_coach",
        "function": "hr_round_simulator",
        "fields": [
            {"name": "question_id", "label": "Question (blank = full set)", "type": "select", "required": False,
             "options": ["", "tell_me", "why_us", "strength_weakness", "why_leave", "salary_expect"]},
        ],
    },
    {
        "id": "coach/clarity-score",
        "feature": "Communication Coach",
        "name": "Clarity Score",
        "description": "Score your spoken English clarity (pace, fillers, jargon) out of 100.",
        "module": "studybuddy.communication_coach",
        "function": "clarity_score",
        "fields": [
            {"name": "transcript", "label": "Speech transcript", "type": "textarea", "required": True},
        ],
    },

    # ---- 14. STAR Builder ----
    {
        "id": "star/story-bank",
        "feature": "STAR Builder",
        "name": "Story Bank Manager",
        "description": "Check your 8-10 STAR stories cover every interview slot; find the gaps.",
        "module": "studybuddy.star_builder",
        "function": "story_bank_manager",
        "fields": [
            {"name": "stories", "label": "Stories (JSON array of {id, title, text, competencies?})", "type": "textarea", "required": True, "json": True},
        ],
    },
    {
        "id": "star/predict-followups",
        "feature": "STAR Builder",
        "name": "Follow-up Predictor",
        "description": "Predict the interviewer's next 3 probing questions for your story.",
        "module": "studybuddy.star_builder",
        "function": "predict_followups",
        "fields": [
            {"name": "story_text", "label": "Your STAR story", "type": "textarea", "required": True},
        ],
    },
    {
        "id": "star/30s-pitch",
        "feature": "STAR Builder",
        "name": "30-Second Pitch Compressor",
        "description": "Compress any STAR story into a ~65-word elevator pitch.",
        "module": "studybuddy.star_builder",
        "function": "thirty_second_pitch",
        "fields": [
            {"name": "story_text", "label": "Your STAR story", "type": "textarea", "required": True},
        ],
    },

    # ---- 15. Learning Roadmaps ----
    {
        "id": "roadmaps/agentic-ai-track",
        "feature": "Learning Roadmaps",
        "name": "Agentic AI Track",
        "description": "The full 14-week Agentic AI roadmap with stages and timelines.",
        "module": "studybuddy.learning_roadmaps",
        "function": "agentic_ai_track",
        "fields": [],
    },
    {
        "id": "roadmaps/project-milestones",
        "feature": "Learning Roadmaps",
        "name": "Project Milestones",
        "description": "Attach a portfolio-worthy project to each roadmap stage.",
        "module": "studybuddy.learning_roadmaps",
        "function": "project_milestones",
        "fields": [
            {"name": "roadmap_stages", "label": "Roadmap stages (JSON array of {stage/name, weeks?, project?})", "type": "textarea", "required": True, "json": True},
        ],
    },
    {
        "id": "roadmaps/weekly-planner",
        "feature": "Learning Roadmaps",
        "name": "Weekly Planner",
        "description": "Turn roadmap stages into a week-by-week calendar plan.",
        "module": "studybuddy.learning_roadmaps",
        "function": "weekly_planner",
        "fields": [
            {"name": "roadmap_stages", "label": "Roadmap stages (JSON array)", "type": "textarea", "required": True, "json": True},
            {"name": "hours_per_week", "label": "Hours per week (1-40, default 6)", "type": "number", "required": False},
        ],
    },

    # ---- 16. Skill Quizzes ----
    {
        "id": "quizzes/cert-exam-mode",
        "feature": "Skill Quizzes",
        "name": "Certification Exam Mode",
        "description": "A timed, weighted quiz like the real certification exam, with pass/fail.",
        "module": "studybuddy.skill_quizzes",
        "function": "certification_exam_mode",
        "fields": [
            {"name": "skill", "label": "Skill", "type": "select", "required": True,
             "options": ["cpp", "data-analysis", "dsa", "git-github", "html-css", "java", "javascript", "linux", "machine-learning", "nodejs", "python", "react", "sql", "statistics", "system-design", "typescript"]},
            {"name": "exam_id", "label": "Exam format", "type": "select", "required": False,
             "options": ["generic", "aws-clf-c02", "aws-saa-c03", "gcp-ace", "az-900", "cka"]},
        ],
    },
    {
        "id": "quizzes/weakness-drill",
        "feature": "Skill Quizzes",
        "name": "Weakness Drill",
        "description": "Auto-generate a drill set targeting your weakest quiz topics.",
        "module": "studybuddy.skill_quizzes",
        "function": "weakness_drill",
        "fields": [
            {"name": "skill", "label": "Skill", "type": "select", "required": True,
             "options": ["cpp", "data-analysis", "dsa", "git-github", "html-css", "java", "javascript", "linux", "machine-learning", "nodejs", "python", "react", "sql", "statistics", "system-design", "typescript"]},
            {"name": "past_results", "label": "Past results (JSON array of {question_id/topic, correct} or grade summary)", "type": "textarea", "required": True, "json": True},
            {"name": "n", "label": "Drill size (3-20, default 10)", "type": "number", "required": False},
        ],
    },
    {
        "id": "quizzes/leaderboard",
        "feature": "Skill Quizzes",
        "name": "Quiz Leaderboard",
        "description": "Rank quiz scores against peers, with your row highlighted.",
        "module": "studybuddy.skill_quizzes",
        "function": "leaderboard",
        "fields": [
            {"name": "entries", "label": "Entries (JSON array of {user_id, name, skill, score_pct, quizzes_taken})", "type": "textarea", "required": True, "json": True},
            {"name": "current_user_id", "label": "Your user ID (optional)", "type": "text", "required": False},
            {"name": "top_n", "label": "Top N to show (default 10)", "type": "number", "required": False},
        ],
    },

    # ---- 17. Certifications ----
    {
        "id": "certs/exam-day-sim",
        "feature": "Certifications",
        "name": "Exam Day Simulator",
        "description": "Full exam-day mock: real question count and time limit for a certification.",
        "module": "studybuddy.cert_guide",
        "function": "exam_day_simulator",
        "fields": [
            {"name": "cert_id", "label": "Certification", "type": "select", "required": True,
             "options": ["aws-clf", "aws-saa", "aws-dva", "az-900", "az-104", "gcp-ace", "gcp-pde", "oci-foundations", "google-data-analytics", "ibm-data-science", "ms-dp-900", "ms-pl-300", "tableau-desktop-associate", "aws-dea", "snowpro-core", "oca-java-se", "pcep-python", "pcap-python", "meta-frontend-dev", "meta-backend-dev", "istqb-ctfl", "comptia-security-plus", "google-cybersecurity", "ceh", "cissp", "comptia-cysa-plus", "ms-sc-900", "aws-scs", "google-project-management", "capm", "pmp", "prince2-foundation", "csm", "ccna", "comptia-network-plus", "jncia-junos", "aws-ans", "cka", "terraform-associate", "docker-dca", "az-400", "aws-mls", "gcp-pmle", "tensorflow-developer"]},
        ],
    },
    {
        "id": "certs/roi",
        "feature": "Certifications",
        "name": "Cert ROI Calculator",
        "description": "Cost vs expected salary bump and payback time for a certification.",
        "module": "studybuddy.cert_guide",
        "function": "cert_roi",
        "fields": [
            {"name": "cert_id", "label": "Certification", "type": "select", "required": True,
             "options": ["aws-clf", "aws-saa", "aws-dva", "az-900", "az-104", "gcp-ace", "gcp-pde", "oci-foundations", "google-data-analytics", "ibm-data-science", "ms-dp-900", "ms-pl-300", "tableau-desktop-associate", "aws-dea", "snowpro-core", "oca-java-se", "pcep-python", "pcap-python", "meta-frontend-dev", "meta-backend-dev", "istqb-ctfl", "comptia-security-plus", "google-cybersecurity", "ceh", "cissp", "comptia-cysa-plus", "ms-sc-900", "aws-scs", "google-project-management", "capm", "pmp", "prince2-foundation", "csm", "ccna", "comptia-network-plus", "jncia-junos", "aws-ans", "cka", "terraform-associate", "docker-dca", "az-400", "aws-mls", "gcp-pmle", "tensorflow-developer"]},
            {"name": "current_lpa", "label": "Your current CTC (LPA)", "type": "number", "required": True},
        ],
    },
    {
        "id": "certs/renewal",
        "feature": "Certifications",
        "name": "Renewal Tracker",
        "description": "Track certification expiry with 90/30/7-day renewal reminders.",
        "module": "studybuddy.cert_guide",
        "function": "renewal_status",
        "fields": [
            {"name": "cert_id", "label": "Certification", "type": "select", "required": True,
             "options": ["aws-clf", "aws-saa", "aws-dva", "az-900", "az-104", "gcp-ace", "gcp-pde", "oci-foundations", "google-data-analytics", "ibm-data-science", "ms-dp-900", "ms-pl-300", "tableau-desktop-associate", "aws-dea", "snowpro-core", "oca-java-se", "pcep-python", "pcap-python", "meta-frontend-dev", "meta-backend-dev", "istqb-ctfl", "comptia-security-plus", "google-cybersecurity", "ceh", "cissp", "comptia-cysa-plus", "ms-sc-900", "aws-scs", "google-project-management", "capm", "pmp", "prince2-foundation", "csm", "ccna", "comptia-network-plus", "jncia-junos", "aws-ans", "cka", "terraform-associate", "docker-dca", "az-400", "aws-mls", "gcp-pmle", "tensorflow-developer"]},
            {"name": "earned_date", "label": "Date earned (YYYY-MM-DD)", "type": "text", "required": True},
        ],
    },

    # ---- 18. PDF Q&A ----
    {
        "id": "pdfqa/answer-across-docs",
        "feature": "PDF Q&A",
        "name": "Answer Across Docs",
        "description": "Ask one question across multiple documents, with per-document evidence.",
        "module": "studybuddy.pdf_qa",
        "function": "answer_across_docs",
        "fields": [
            {"name": "query", "label": "Your question", "type": "text", "required": True},
            {"name": "docs", "label": "Documents (JSON array of {title, chunks})", "type": "textarea", "required": True, "json": True},
            {"name": "top_k", "label": "Chunks per doc (default 3)", "type": "number", "required": False},
        ],
    },
    {
        "id": "pdfqa/verify-citations",
        "feature": "PDF Q&A",
        "name": "Citation Verifier",
        "description": "Check that every claim in an AI answer carries a citation marker.",
        "module": "studybuddy.pdf_qa",
        "function": "verify_citations",
        "fields": [
            {"name": "answer_dict", "label": "Answer (JSON dict with an \"answer\" key)", "type": "textarea", "required": True, "json": True},
        ],
    },
    {
        "id": "pdfqa/qa-to-notes",
        "feature": "PDF Q&A",
        "name": "Q&A to Notes",
        "description": "Export Q&A pairs into structured markdown study notes.",
        "module": "studybuddy.pdf_qa",
        "function": "qa_to_notes",
        "fields": [
            {"name": "qa_pairs", "label": "Q&A pairs (JSON array of {question, answer, citations?})", "type": "textarea", "required": True, "json": True},
            {"name": "title", "label": "Notes title", "type": "text", "required": False},
        ],
    },

    # ---- 19. arXiv / DOI Import ----
    {
        "id": "arxiv/bulk-import",
        "feature": "arXiv Import",
        "name": "Bulk Paper Import",
        "description": "Import up to 25 papers at once from pasted arXiv IDs or DOIs.",
        "module": "studybuddy.arxiv_import",
        "function": "bulk_import",
        "fields": [
            {"name": "ids_text", "label": "arXiv IDs / DOIs (one per line or comma-separated)", "type": "textarea", "required": True},
            {"name": "max_ids", "label": "Max papers to import (1-25, default 10)", "type": "number", "required": False},
        ],
    },
    {
        "id": "arxiv/related-papers",
        "feature": "arXiv Import",
        "name": "Related Paper Discovery",
        "description": "Discover related papers through the citation graph (cited by + references).",
        "module": "studybuddy.arxiv_import",
        "function": "related_papers",
        "fields": [
            {"name": "openalex_id", "label": "OpenAlex paper ID", "type": "text", "required": True},
            {"name": "max_results", "label": "Max results (default 10)", "type": "number", "required": False},
        ],
    },
    {
        "id": "arxiv/reading-queue",
        "feature": "arXiv Import",
        "name": "Prioritized Reading Queue",
        "description": "A relevance-ranked reading queue with time estimates per paper.",
        "module": "studybuddy.arxiv_import",
        "function": "reading_queue",
        "fields": [
            {"name": "papers", "label": "Papers (JSON array of {title, year, abstract?, id})", "type": "textarea", "required": True, "json": True},
            {"name": "interests", "label": "Your interests (JSON array of keywords, optional)", "type": "textarea", "required": False, "json": True},
            {"name": "minutes_per_paper", "label": "Minutes per paper (default 45)", "type": "number", "required": False},
        ],
    },

    # ---- 20. Citation Formatter ----
    {
        "id": "citations/in-text",
        "feature": "Citation Formatter",
        "name": "In-Text Citation Snippets",
        "description": "Copy-paste-ready in-text and LaTeX citation snippets for a paper.",
        "module": "studybuddy.citation_fmt",
        "function": "in_text_citation",
        "fields": [
            {"name": "meta", "label": "Paper metadata (JSON: {authors: [...], year, title})", "type": "textarea", "required": True, "json": True},
            {"name": "style", "label": "Citation style", "type": "select", "required": False,
             "options": ["apa", "mla", "ieee", "vancouver", "chicago"]},
        ],
    },
    {
        "id": "citations/journal-style",
        "feature": "Citation Formatter",
        "name": "Journal Style Matcher",
        "description": "Paste a sample reference; it guesses which citation style it follows.",
        "module": "studybuddy.citation_fmt",
        "function": "match_journal_style",
        "fields": [
            {"name": "sample_citation", "label": "Sample reference text", "type": "textarea", "required": True},
        ],
    },
    {
        "id": "citations/find-duplicates",
        "feature": "Citation Formatter",
        "name": "Duplicate Reference Detector",
        "description": "Flag near-duplicate references in your bibliography.",
        "module": "studybuddy.citation_fmt",
        "function": "find_duplicates",
        "fields": [
            {"name": "references", "label": "References (JSON array of strings or {title} dicts)", "type": "textarea", "required": True, "json": True},
            {"name": "threshold", "label": "Similarity threshold 0.5-1.0 (default 0.85)", "type": "number", "required": False},
        ],
    },
]
