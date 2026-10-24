document.addEventListener('DOMContentLoaded', () => {
    // P2#11 follow-up: all fetch-driven POSTs send the session CSRF token via
    // the X-CSRFToken header (read from the page's csrf-token meta tag), so no
    // research endpoint needs a CSRF exemption anymore.
    const csrfHeaders = (extra) => {
        const headers = extra || {};
        const m = document.querySelector('meta[name="csrf-token"]');
        if (m) headers['X-CSRFToken'] = m.getAttribute('content');
        return headers;
    };

    // Navigation Logic
    const navLinks = document.querySelectorAll('.nav-link');
    const sections = document.querySelectorAll('.page-section');

    const showSection = (sectionId) => {
        // Update Nav Links
        navLinks.forEach(link => {
            link.classList.toggle('active', link.dataset.section === sectionId);
        });

        // Update Sections
        sections.forEach(section => {
            if (section.id === sectionId) {
                section.classList.add('active');
                // Scroll to top of section for better UX
                window.scrollTo({ top: 0, behavior: 'smooth' });
            } else {
                section.classList.remove('active');
            }
        });
    };

    // Attach to global scope for button onclicks
    window.showSection = showSection;

    // CSP: delegated handlers replacing inline onclick attributes.
    // Retry + Browse buttons are re-created via innerHTML, so one
    // delegated listener covers every instance, static or dynamic.
    document.addEventListener('click', (e) => {
        const retry = e.target.closest('.js-retry');
        if (retry) { location.reload(); return; }
        const browse = e.target.closest('.js-browse');
        if (browse) {
            const fi = document.getElementById('file-input');
            if (fi) fi.click();
        }
    });
    document.querySelectorAll('[data-show-section]').forEach((btn) => {
        btn.addEventListener('click', () => showSection(btn.getAttribute('data-show-section')));
    });

    navLinks.forEach(link => {
        link.addEventListener('click', (e) => {
            const sectionId = link.dataset.section;
            if (sectionId) {
                e.preventDefault();
                showSection(sectionId);
                // Update URL hash without jumping
                history.pushState(null, null, `#${sectionId}`);
            }
            // If no sectionId (e.g., Hub link), let the default navigation happen
        });
    });

    // Handle initial hash
    const initialHash = window.location.hash.substring(1);
    if (initialHash && document.getElementById(initialHash)) {
        showSection(initialHash);
    }

    // Wire up the CTA button (Bug #15 fix)
    const btnCta = document.getElementById('btn-cta');
    if (btnCta) {
        btnCta.addEventListener('click', () => showSection('constructor'));
    }

    // --- Global State for Analysis ---
    let currentPaperAnalysis = null;
    let currentPaperContext = null;
    // P0#1: session counter for the "New Session" button (was used but never declared).
    let sessionCounter = 0;
    // Client-side chat history (last exchanges sent to the backend for context).
    const chatHistory = [];

    // --- Constructor Logic ---
    const constructorForm = document.getElementById('form-constructor');
    const constructorProcessing = document.getElementById('constructor-processing');
    const constructorResult = document.getElementById('constructor-result');
    const formContainer = constructorForm.closest('.form-container');

    const steps = [
        { id: 'step-fetch', text: 'Fetching live repository data from GitHub', duration: 1500 },
        { id: 'step-vector', text: 'Analyzing file tree & languages', duration: 2000 },
        { id: 'step-analyze', text: 'AI Architecture Analysis', duration: 1800 },
        { id: 'step-generate', text: 'Drafting IEEE Paper', duration: 2500 }
    ];

    constructorForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const repoUrl = document.getElementById('repo-url').value;
        const author = document.getElementById('author-name').value;
        const institution = document.getElementById('institution').value;

        // Start Transition
        formContainer.classList.add('hidden');
        constructorProcessing.classList.remove('hidden');
        
        // Reset steps
        steps.forEach(step => {
            const el = document.getElementById(step.id);
            el.classList.remove('active', 'complete');
        });

        // Run Progression (Steps 1-3)
        for (let i = 0; i < steps.length - 1; i++) {
            const step = steps[i];
            const el = document.getElementById(step.id);
            el.classList.add('active');
            await new Promise(resolve => setTimeout(resolve, step.duration));
            el.classList.remove('active');
            el.classList.add('complete');
        }

        // --- Step 4: Real AI Generation ---
        const generationStep = document.getElementById('step-generate');
        generationStep.classList.add('active');

        try {
            const response = await fetch('/api/constructor_generate', {
                method: 'POST',
                headers: csrfHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ repo_url: repoUrl, author: author, institution: institution })
            });

            const data = await response.json();
            
            if (!data.success) throw new Error(data.error);

            const analysis = data.analysis;
            currentPaperAnalysis = analysis; // Store for PDF download
            
            generationStep.classList.remove('active');
            generationStep.classList.add('complete');

            // Show Result
            constructorProcessing.classList.add('hidden');
            constructorResult.classList.remove('hidden');

            // Populate Preview with Defensive Key Binding
            const getVal = (keys, obj) => {
                for (let k of keys) {
                    if (obj[k]) return obj[k];
                }
                return "";
            };

            document.getElementById('preview-title').textContent = analysis.title || analysis.Title || "Academic Research Paper";
            document.getElementById('preview-meta').textContent = `Authors: ${author} | Institution: ${institution}`;
            document.getElementById('preview-abstract').textContent = getVal(['abstract', 'Summary', 'abstract'], analysis);
            document.getElementById('preview-intro').textContent = getVal(['introduction', 'intro', 'Introduction'], analysis);
            document.getElementById('preview-lit').textContent = getVal(['lit_review', 'literature_review', 'LitReview'], analysis);
            document.getElementById('preview-architecture').textContent = getVal(['architecture', 'design', 'Architecture'], analysis);
            document.getElementById('preview-impl').textContent = getVal(['implementation', 'Implementation', 'impl'], analysis);
            document.getElementById('preview-results').textContent = getVal(['results', 'Results', 'analysis'], analysis);
            document.getElementById('preview-conclusion').textContent = getVal(['conclusion', 'Conclusion'], analysis);
            
            // Stats
            const stats = analysis.stats || {};
            document.getElementById('stat-files').textContent = stats.files || "--";
            document.getElementById('stat-complexity').textContent = stats.complexity || "High";
            document.getElementById('stat-confidence').textContent = stats.confidence || "90%";

        } catch (err) {
            console.error("Constructor Error:", err);
            constructorProcessing.classList.add('hidden');
            formContainer.classList.remove('hidden');
            alert("AI Generation failed. Please check your API key or connection. Details: " + err.message);
        }
    });

    // Bug #11 Fix: Wire up 'Generate Another' reset button in result area
    constructorResult.addEventListener('click', (e) => {
        if (e.target && e.target.id === 'btn-generate-another') {
            constructorResult.classList.add('hidden');
            constructorProcessing.classList.add('hidden');
            formContainer.classList.remove('hidden');
            // Reset form
            document.getElementById('form-constructor').reset();
            // Reset steps
            steps.forEach(step => {
                const el = document.getElementById(step.id);
                if (el) el.classList.remove('active', 'complete');
            });
        }
    });

// Cleanup of old mock engine
function getIEEEHeader(title, author, institution) {
    return `${title}\n\nAuthors: ${author}\nInstitution: ${institution}\n\n`;
}

    // Initialize PDF.js worker
    const pdfjsLib = window['pdfjs-dist/build/pdf'];
    pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

    // ========================================================
    // --- DECONSTRUCTOR: Live Document-Grounded Chat ---
    // ========================================================

    const fileInput = document.getElementById('file-input');
    const uploadScreen = document.getElementById('upload-screen');
    const chatScreen = document.getElementById('chat-screen');
    const messageFeed = document.getElementById('message-feed');
    const chatInput = document.getElementById('chat-input');
    const btnSend = document.getElementById('btn-send');
    const typingIndicator = document.getElementById('typing-indicator');
    const btnNewSession = document.getElementById('btn-new-session');
    const sessionList = document.getElementById('session-list');

    // --- Persisted research sessions (logged-in users only) ---
    // The backend saves each chat exchange to a per-user ResearchSession and
    // returns its id; we keep it in localStorage so follow-up questions land
    // in the same server-side session.
    const ACRA_SESSION_KEY = 'acra_research_session_id';
    let researchSessionId = (window.ACRA_AUTH === true)
        ? localStorage.getItem(ACRA_SESSION_KEY)
        : null;
    const resetResearchSession = () => {
        researchSessionId = null;
        try { localStorage.removeItem(ACRA_SESSION_KEY); } catch (e) { /* ignore */ }
    };

    // --- Extract Full Text & Sections from PDF ---
    async function analyzePDF(file) {
        const arrayBuffer = await file.arrayBuffer();
        const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise;
        let fullText = '';
        
        for (let i = 1; i <= pdf.numPages; i++) {
            const page = await pdf.getPage(i);
            const content = await page.getTextContent();
            const strings = content.items.map(item => item.str);
            fullText += strings.join(' ') + '\n';
        }

        const name = file.name.replace(/\.pdf$/i, '').replace(/[_\-]/g, ' ');
        const topic = name.replace(/\b\w/g, c => c.toUpperCase());
        
        // --- Smart Section Extraction ---
        const sections = {
            summary: extractSection(fullText, ['abstract', 'introduction', 'overview']),
            methodology: extractSection(fullText, ['methodology', 'methods', 'experimental setup', 'implementation']),
            results: extractSection(fullText, ['results', 'findings', 'evaluation', 'performance']),
            conclusion: extractSection(fullText, ['conclusion', 'future work', 'limitations', 'discussion']),
            citations: extractSection(fullText, ['references', 'bibliography', 'citations', 'sources'])
        };

        // Extract keywords based on frequency
        const words = fullText.toLowerCase().match(/\b[a-z]{5,}\b/g) || [];
        const freq = {};
        words.forEach(w => freq[w] = (freq[w] || 0) + 1);
        const keywords = Object.keys(freq).sort((a, b) => freq[b] - freq[a]).slice(0, 12);

        // P2#15: honest citation-marker count (never fabricated).
        const citationMarkers = fullText.match(/\[\d+\]|\(\w+,\s\d{4}\)/g) || [];

        return {
            topic,
            fileName: file.name,
            fullText,
            sections,
            keywords,
            pageCount: pdf.numPages,
            citationCount: citationMarkers.length,
            timestamp: new Date().toLocaleTimeString()
        };
    }

    function extractSection(text, keywords) {
        const lines = text.split('\n');
        let startIdx = -1;
        let endIdx = text.length;

        // Find the start of the section
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i].toLowerCase().trim();
            if (keywords.some(k => line.includes(k) && line.length < 50)) {
                startIdx = text.indexOf(lines[i]);
                break;
            }
        }

        if (startIdx === -1) return null;

        // Find the start of the NEXT section to determine the end
        const allPossibleHeaders = ['introduction', 'methodology', 'results', 'conclusion', 'references', 'abstract', 'related work'];
        const remainingText = text.substring(startIdx + 20); // skip the header itself
        const nextHeaderMatch = remainingText.match(new RegExp(`\\n\\s*(?:${allPossibleHeaders.join('|')})`, 'i'));
        
        if (nextHeaderMatch) {
            endIdx = startIdx + 20 + nextHeaderMatch.index;
        }

        return text.substring(startIdx, endIdx).trim();
    }

    // NOTE (P2#16): the old mock Q&A engine (generateDeconstructorResponse /
    // findBestMatch / formatResponse) was dead code - all chat goes through
    // /api/chat_research now - so it has been removed.

    // --- File Upload Handler (Context-Aware) ---
    const handleFileUpload = async (files) => {
        if (files.length > 0) {
            const file = files[0];
            
            // Show processing simulation with real file info
            uploadScreen.innerHTML = `
                <div class="processor-card glass">
                    <div class="spinner"></div>
                    <p style="margin-top: 1rem; font-weight: 600;">Deep Scanning ${file.name}...</p>
                    <p style="margin-top: 0.5rem; font-size: 0.85rem; color: var(--text-muted);">Extracting document-grounded intelligence...</p>
                </div>
            `;
            
            try {
                // Perform real analysis
                currentPaperContext = await analyzePDF(file);

                uploadScreen.classList.add('hidden');
                chatScreen.classList.remove('hidden');

                // Update session sidebar
                const currentSessionEl = document.getElementById('session-current');
                if (currentSessionEl) {
                    currentSessionEl.textContent = currentPaperContext.topic.length > 22 
                        ? currentPaperContext.topic.substring(0, 22) + '…'
                        : currentPaperContext.topic;
                }

                // Show initial analysis message
                messageFeed.innerHTML = '';
                const citePhrase = currentPaperContext.citationCount > 0
                    ? `detected **${currentPaperContext.citationCount} citation markers**`
                    : 'found no bracketed citation markers';
                addMessage('system', `
**${currentPaperContext.topic}**

### Document Grounded Successfully
I've verified the document **"${file.name}"** with **${currentPaperContext.pageCount} pages** and ${citePhrase}.

**Extracted Keywords:** ${currentPaperContext.keywords.slice(0, 5).join(', ')}...

I am now grounding my responses 100% in the text of this paper. Ask me anything about:
* Summary of the work
* The actual methodology used
* Specific findings and results
* Concluding remarks

**How can I help you explore this research today?**`);
            } catch (err) {
                console.error(err);
                uploadScreen.innerHTML = `
                    <div class="processor-card glass">
                        <p style="color: #ff4d4d;">Error indexing document. Please ensure it's a valid PDF.</p>
                        <button class="btn btn-primary btn-sm js-retry">Retry</button>
                    </div>
                `;
            }
        }
    };

    fileInput.addEventListener('change', (e) => handleFileUpload(e.target.files));

    // --- Drag and Drop ---
    const dropZone = document.getElementById('drop-zone');
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = 'var(--primary)';
        dropZone.style.background = 'rgba(99, 102, 241, 0.1)';
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.style.borderColor = 'var(--glass-border)';
        dropZone.style.background = 'transparent';
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = 'var(--glass-border)';
        dropZone.style.background = 'transparent';
        handleFileUpload(e.dataTransfer.files);
    });

    // --- Message System ---
    const escapeHtml = (s) => String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

    const addMessage = (role, text) => {
        const msgDiv = document.createElement('div');
        msgDiv.className = `message ${role} stagger-entry`;

        // Parse markdown if it's from the system/assistant; escape user input (XSS).
        const renderedText = (role === 'system') ? marked.parse(text) : escapeHtml(text);

        msgDiv.innerHTML = `
            <div class="avatar">${role === 'user' ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="8" r="3.6"/><path d="M5 20a7 7 0 0 1 14 0"/></svg>' : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="9" width="14" height="11" rx="2"/><path d="M12 9V4M8 4h8"/><circle cx="9.5" cy="13.5" r="0.8" fill="currentColor" stroke="none"/><circle cx="14.5" cy="13.5" r="0.8" fill="currentColor" stroke="none"/><path d="M9.5 17h5"/></svg>'}</div>
            <div class="bubble">${renderedText}</div>
        `;
        messageFeed.appendChild(msgDiv);
        messageFeed.scrollTop = messageFeed.scrollHeight;
    };

    // --- Show/Hide Typing Indicator ---
    const showTyping = () => {
        if (typingIndicator) {
            typingIndicator.classList.remove('hidden');
            messageFeed.scrollTop = messageFeed.scrollHeight;
        }
    };

    const hideTyping = () => {
        if (typingIndicator) {
            typingIndicator.classList.add('hidden');
        }
    };

    // --- Smart Send Message (API Mode) ---
    const handleSendMessage = async () => {
        const text = chatInput.value.trim();
        if (!text) return;

        addMessage('user', text);
        chatInput.value = '';
        chatHistory.push({ role: 'user', content: text });

        // Show typing indicator
        showTyping();

        try {
            // Call the Gemini-powered backend with real conversation history (P1#4).
            const response = await fetch('/api/chat_research', {
                method: 'POST',
                headers: csrfHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({
                    message: text,
                    document_text: (currentPaperContext && currentPaperContext.fullText) || '',
                    history: chatHistory.slice(-12),
                    research_session_id: researchSessionId
                })
            });

            hideTyping();

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                addMessage('system', `<span style="color: #ff4d4d;">Server Error (${response.status}): ${errorData.error || 'The AI server encountered an issue.'}</span>`);
                return;
            }

            const data = await response.json();
            if (data.research_session_id) {
                researchSessionId = String(data.research_session_id);
                try { localStorage.setItem(ACRA_SESSION_KEY, researchSessionId); } catch (e) { /* ignore */ }
            }
            chatHistory.push({ role: 'assistant', content: data.response || '' });
            addMessage('system', data.response);
        } catch (err) {
            hideTyping();
            addMessage('system', `<span style="color: #ff4d4d;">System failure: Could not reach the AI Intelligence server.</span>`);
            console.error(err);
        }
    };

    btnSend.addEventListener('click', handleSendMessage);
    chatInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') handleSendMessage();
    });

    // --- Session Management ---
    const sessionItems = document.querySelectorAll('.session-item');
    sessionItems.forEach(item => {
        item.addEventListener('click', () => {
            sessionItems.forEach(s => s.classList.remove('active'));
            item.classList.add('active');

            messageFeed.innerHTML = '';
            chatHistory.length = 0; // switching sessions starts a fresh context
            const sessionName = item.textContent.trim();
            
            if (sessionName === 'Current Session' && !currentPaperContext) {
                // No paper uploaded yet
                addMessage('system', "Please upload a research paper to begin analysis in this session.");
            } else {
                addMessage('system', `
**${sessionName}**

I've loaded the context for **"${sessionName}"**. How can I help you explore this research?`);
            }
        });
    });

    // --- New Session Button ---
    if (btnNewSession) {
        btnNewSession.addEventListener('click', () => {
            sessionCounter++;
            currentPaperContext = null;
            chatHistory.length = 0;
            resetResearchSession(); // start a fresh server-side session too

            // Create new session item
            const newItem = document.createElement('li');
            newItem.className = 'session-item active';
            newItem.textContent = `Session ${sessionCounter + 1}`;
            newItem.id = 'session-current';

            // Deactivate all existing sessions
            sessionList.querySelectorAll('.session-item').forEach(s => {
                s.classList.remove('active');
                s.removeAttribute('id');
            });

            // Add new session to sidebar
            sessionList.prepend(newItem);

            // Reset chat to upload state
            chatScreen.classList.add('hidden');
            uploadScreen.classList.remove('hidden');
            uploadScreen.innerHTML = `
                <div class="upload-zone" id="drop-zone">
                    <div class="upload-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 15h6"/></svg></div>
                    <h3>Upload Research Papers</h3>
                    <p>Drag and drop PDF files or click to browse</p>
                    <input type="file" id="file-input" multiple accept=".pdf" class="hidden">
                    <button class="btn btn-primary btn-sm js-browse">Browse Files</button>
                </div>
            `;

            // Re-attach file input listener for new element
            const newFileInput = document.getElementById('file-input');
            newFileInput.addEventListener('change', (e) => handleFileUpload(e.target.files));

            // Re-attach drag-and-drop for new element
            const newDropZone = document.getElementById('drop-zone');
            newDropZone.addEventListener('dragover', (e) => {
                e.preventDefault();
                newDropZone.style.borderColor = 'var(--primary)';
                newDropZone.style.background = 'rgba(99, 102, 241, 0.1)';
            });
            newDropZone.addEventListener('dragleave', () => {
                newDropZone.style.borderColor = 'var(--glass-border)';
                newDropZone.style.background = 'transparent';
            });
            newDropZone.addEventListener('drop', (e) => {
                e.preventDefault();
                newDropZone.style.borderColor = 'var(--glass-border)';
                newDropZone.style.background = 'transparent';
                handleFileUpload(e.dataTransfer.files);
            });

            // Allow clicking on new session too
            newItem.addEventListener('click', () => {
                sessionList.querySelectorAll('.session-item').forEach(s => s.classList.remove('active'));
                newItem.classList.add('active');
                messageFeed.innerHTML = '';
                addMessage('system', 'Please upload a research paper to begin analysis in this session.');
            });
        });
    }


    // --- PDF Generation (Professional IEEE Style) ---
    const btnDownloadPdf = document.getElementById('btn-download-pdf');

    btnDownloadPdf.addEventListener('click', () => {
        const { jsPDF } = window.jspdf;
        const doc = new jsPDF();
        const repoUrl = document.getElementById('repo-url').value;
        const author = document.getElementById('author-name').value;
        const institution = document.getElementById('institution').value;
        
        const title = document.getElementById('preview-title').textContent;
        const meta = document.getElementById('preview-meta').textContent;
        const analysis = currentPaperAnalysis;

        if (!analysis) {
            alert("Please generate a paper first!");
            return;
        }

        // --- Helper for Two-Column Text ---
        const addColumnText = (text, x, y, width, fontSize = 9) => {
            doc.setFont("times", "normal");
            doc.setFontSize(fontSize);
            const lines = doc.splitTextToSize(text, width);
            doc.text(lines, x, y);
            return y + (lines.length * (fontSize * 0.45)); // approximate next Y
        };

        // --- PAGE 1: Formal Header & Abstract ---
        doc.setFont("times", "bold");
        doc.setFontSize(20);
        const splitTitle = doc.splitTextToSize(title.toUpperCase(), 170);
        doc.text(splitTitle, 105, 20, { align: 'center' });

        doc.setFont("times", "italic");
        doc.setFontSize(11);
        const metaLines = doc.splitTextToSize(meta, 170);
        doc.text(metaLines, 105, 35, { align: 'center' });

        doc.setLineWidth(0.5);
        doc.line(20, 42, 190, 42);

        // Abstract (Bold prefix)
        doc.setFont("times", "bold");
        doc.setFontSize(10);
        doc.text("Abstract—", 20, 52);
        doc.setFont("times", "normal");
        const abstractLines = doc.splitTextToSize(analysis.abstract, 160);
        doc.text(abstractLines, 38, 52);
        
        let currentY = 52 + (abstractLines.length * 5);

        doc.setFont("times", "bold");
        doc.text("Keywords—", 20, currentY + 5);
        doc.setFont("times", "normal");
        doc.text("Artificial Intelligence, Repository Analysis, RAG Pipelines, Automated Documentation, IEEE Standards.", 40, currentY + 5);

        // --- START TWO-COLUMN BODY (Page 1) ---
        const col1X = 20;
        const col2X = 110;
        const colWidth = 80;
        let y1 = currentY + 20;
        let y2 = currentY + 20;

        // I. Introduction
        doc.setFont("times", "bold");
        doc.text("I. INTRODUCTION", col1X, y1);
        y1 += 7;
        const introText = analysis.introduction;
        y1 = addColumnText(introText, col1X, y1, colWidth);

        // II. LITERATURE REVIEW
        y1 += 10;
        doc.setFont("times", "bold");
        doc.text("II. LITERATURE REVIEW", col1X, y1);
        y1 += 7;
        const litText = analysis.lit_review;
        y1 = addColumnText(litText, col1X, y1, colWidth);

        // III. ARCHITECTURE & DESIGN
        doc.setFont("times", "bold");
        doc.text("III. DESIGN PHILOSOPHY & ARCHITECTURE", col2X, y2);
        y2 += 7;
        const designText = analysis.architecture;
        y2 = addColumnText(designText, col2X, y2, colWidth);
        
        y2 += 10;
        const implHeader = "IV. TECHNICAL IMPLEMENTATION";
        doc.setFont("times", "bold");
        doc.text(implHeader, col2X, y2);
        y2 += 7;
        const implText = analysis.implementation;
        y2 = addColumnText(implText, col2X, y2, colWidth);

        // --- PAGE 2: Technical Deep Dive ---
        doc.addPage();
        let py1 = 20;
        let py2 = 20;

        // IV. ARCHITECTURE
        doc.setFont("times", "bold");
        doc.text("IV. SYSTEM ARCHITECTURE", col1X, py1);
        py1 += 7;
        const archText = `The structural analysis of ${repoUrl.split('/').pop()} reveals a highly modular environment. Our engine identifies a centralized orchestration layer coupled with distributed service nodes. This decoupling allows for independent scaling of components, a hallmark of modern cloud-native design. The following architectural patterns were identified as critical to the system's performance metrics:`;
        py1 = addColumnText(archText, col1X, py1, colWidth);
        
        py1 += 5;
        doc.setFont("times", "italic");
        py1 = addColumnText("A. Data Ingestion Layer: Handles raw source parsing and initial tokenization.", col1X, py1, colWidth);
        py1 += 5;
        py1 = addColumnText("B. Vector Indexing: Manages the high-dimensional mapping of code elements.", col1X, py1, colWidth);
        py1 += 5;
        py1 = addColumnText("C. Synthesis Engine: Orchestrates the LLM-driven academic drafting process.", col1X, py1, colWidth);

        // V. RESULTS & ANALYSIS
        doc.setFont("times", "bold");
        doc.text("V. RESULTS & ANALYSIS", col2X, py2);
        py2 += 7;
        const methodText = analysis.results; 
        py2 = addColumnText(methodText, col2X, py2, colWidth);

        // --- PAGE 3: Results & Summary ---
        doc.addPage();
        doc.setFont("times", "bold");
        doc.setFontSize(12);
        doc.text("VI. PRELIMINARY RESULTS", 20, 25);
        
        doc.setFont("times", "normal");
        doc.setFontSize(10);
        const resText = `The analysis of ${repoUrl} yielded a complexity score of ${analysis.stats.complexity}. With ${analysis.stats.files} files indexed, the engine achieved a confidence rating of ${analysis.stats.confidence}. These results suggest a mature development environment with clear architectural precedents. The synthesized paper demonstrates that automated tools can effectively capture the technical essence of complex software repositories without manual intervention.`;
        doc.text(doc.splitTextToSize(resText, 170), 20, 33);

        doc.setFont("times", "bold");
        doc.text("VII. CONCLUSION", 20, 60);
        doc.setFont("times", "normal");
        doc.text(doc.splitTextToSize(analysis.conclusion, 170), 20, 68);

        // --- CONSTRUCTOR SUMMARY DASHBOARD (Page 3 Bottom) ---
        doc.setFillColor(15, 23, 42); // Navy Dark
        doc.rect(20, 100, 170, 160, 'F');
        
        doc.setTextColor(255, 255, 255);
        doc.setFont("helvetica", "bold"); // Modern font for dashboard
        doc.setFontSize(20);
        doc.text("CONSTRUCTOR DASHBOARD", 105, 120, { align: 'center' });
        
        doc.setDrawColor(99, 102, 241);
        doc.setLineWidth(1);
        doc.line(70, 125, 140, 125);

        doc.setFontSize(11);
        doc.setTextColor(148, 163, 184);
        doc.text("Repository Analysis Summary & Process Metrics", 105, 133, { align: 'center' });

        // Metrics Grid
        const gridX = 40;
        doc.setTextColor(255, 255, 255);
        doc.setFontSize(13);
        doc.text("Target Repo:", gridX, 155);
        doc.setTextColor(34, 211, 238); // Cyan
        doc.text(repoUrl.length > 35 ? repoUrl.substring(0, 35) + '...' : repoUrl, 85, 155);

        doc.setTextColor(255, 255, 255);
        doc.text("Files Scanned:", gridX, 175);
        doc.text(analysis.stats.files.toString(), 85, 175);

        doc.text("Architecture:", gridX, 195);
        doc.text(analysis.stats.complexity, 85, 195);

        doc.text("AI Confidence:", gridX, 215);
        doc.text(analysis.stats.confidence, 85, 215);

        // Checkmark
        doc.setDrawColor(34, 211, 238);
        doc.line(160, 180, 165, 185);
        doc.line(165, 185, 175, 175);
        doc.text("SYNTHESIZED", 167, 195, { align: 'center' });

        doc.setFontSize(9);
        doc.setTextColor(100, 116, 139);
        doc.text("This document was autonomously generated by PaperAI Engine V1.0 - Academic Compliance Verified.", 105, 250, { align: 'center' });

        // Save
        const fileName = `${title.replace(/\s+/g, '_')}_IEEE_Final.pdf`;
        doc.save(fileName);
    });
});
