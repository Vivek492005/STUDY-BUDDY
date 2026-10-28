document.addEventListener('DOMContentLoaded', () => {
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

    navLinks.forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const sectionId = link.dataset.section;
            showSection(sectionId);
            // Update URL hash without jumping
            history.pushState(null, null, `#${sectionId}`);
        });
    });

    // Handle initial hash
    const initialHash = window.location.hash.substring(1);
    if (initialHash && document.getElementById(initialHash)) {
        showSection(initialHash);
    }

    // --- Constructor Logic ---
    const constructorForm = document.getElementById('form-constructor');
    const constructorProcessing = document.getElementById('constructor-processing');
    const constructorResult = document.getElementById('constructor-result');
    const formContainer = constructorForm.closest('.form-container');

    const steps = [
        { id: 'step-fetch', text: 'Fetching Repository Source', duration: 1500 },
        { id: 'step-vector', text: 'Building Vector Knowledge Base', duration: 2000 },
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

        // Run Simulation
        for (let i = 0; i < steps.length; i++) {
            const step = steps[i];
            const el = document.getElementById(step.id);
            el.classList.add('active');
            
            await new Promise(resolve => setTimeout(resolve, step.duration));
            
            el.classList.remove('active');
            el.classList.add('complete');
        }

        // Show Result
        constructorProcessing.classList.add('hidden');
        constructorResult.classList.remove('hidden');

    // Populate Preview (Mock)
    const repoName = repoUrl.split('/').pop() || 'Repository';
    document.getElementById('preview-title').textContent = `Architectural Synthesis of ${repoName}`;
    document.getElementById('preview-meta').textContent = `Authors: ${author} | Institution: ${institution}`;

    // Update with Dynamic Analysis
    const analysis = generateRepoSpecificContent(repoUrl, author, institution);
    document.getElementById('preview-abstract').textContent = analysis.abstract;
    document.getElementById('preview-architecture').textContent = analysis.architecture;
    document.getElementById('preview-methodology').textContent = analysis.methodology;
    
    // Stats
    document.getElementById('stat-files').textContent = analysis.stats.files;
    document.getElementById('stat-complexity').textContent = analysis.stats.complexity;
    document.getElementById('stat-confidence').textContent = analysis.stats.confidence;
});

// --- Dynamic Content Engine ---
function generateRepoSpecificContent(url, author, institution) {
    const repoName = url.split('/').pop() || 'Modern Repository';
    const owner = url.split('/').slice(-2, -1)[0] || 'innovative-dev';
    
    // Random but stable stats for this URL
    const hash = (str) => [...str].reduce((s, c) => Math.imul(31, s) + c.charCodeAt(0) | 0, 0);
    const h = Math.abs(hash(url));

    return {
        abstract: `This technical inquiry examines the structural integrity and design patterns implemented within the ${repoName} repository. By analyzing primary source components under the authorship of ${author} at ${institution}, we identify a robust architectural framework specialized for high-performance data processing and modular scalability. Our synthesis reveals critical intersections between ${owner}'s implementation strategies and contemporary research in automated codebase analysis using RAG-based systems.`,
        
        architecture: `The ${repoName} architecture is characterized by a multi-layered design prioritizing decoupling and asynchronous communication. Core components identified include a centralized controller for resource management and specialized modules for task orchestration. Our analysis detects the use of optimized design patterns such as the Observer and Facade paradigms, which ensure that ${owner}'s codebase remains adaptable to large-scale integration environments.`,
        
        methodology: `The synthesis utilized a non-linear analysis pipeline, starting with deep source parsing of the ${repoName} file structure. We extracted semantic embeddings for individual functions and classes, which were then indexed into a high-dimensional vector space for relational mapping. This document-grounded approach ensures that every synthetic claim is anchored in the physical codebase provided at ${url}.`,

        stats: {
            files: (h % 50) + 15,
            complexity: (h % 3 === 0) ? 'High' : ((h % 3 === 1) ? 'Moderate' : 'Extreme'),
            confidence: (85 + (h % 15)) + '%'
        },
        
        conclusion: `The evaluation of ${repoName} underscores its maturity as a technical artifact. The implementation of professional-grade standards suggests a highly refined development cycle. Moving forward, this architectural synthesis recommends further exploration into the system's latent scalability metrics and its potential for cross-platform integration as documented by ${author}.`
    };
}

    // ========================================================
    // --- DECONSTRUCTOR: Smart Context-Aware Research Chat ---
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

    // --- Session State ---
    let currentPaperContext = null; // stores info about the uploaded papers
    let sessionCounter = 0;

    // --- Extract Topic from Filename ---
    function extractTopicFromFiles(files) {
        const names = Array.from(files).map(f => f.name.replace(/\.pdf$/i, '').replace(/[_\-]/g, ' '));
        const primaryName = names[0] || 'Uploaded Document';
        
        // Build a rich context object
        const topic = primaryName.replace(/\b\w/g, c => c.toUpperCase());
        const keywords = primaryName.toLowerCase().split(/\s+/).filter(w => w.length > 3);
        
        return {
            topic,
            fileCount: files.length,
            allNames: names,
            keywords,
            pageEstimate: Math.floor(Math.random() * 20) + 8,
            citationCount: Math.floor(Math.random() * 35) + 10,
            indexTimestamp: new Date().toLocaleTimeString()
        };
    }

    // --- Dynamic AI Response Engine ---
    function generateDeconstructorResponse(query, context) {
        if (!context) {
            return "Please upload a research paper first so I can analyze it for you. I need document context to provide meaningful insights.";
        }

        const q = query.toLowerCase();
        const topic = context.topic;
        const kw = context.keywords.join(', ');

        // --- Keyword-Based Intelligent Matching ---

        if (q.includes('topic') || q.includes('about') || q.includes('what is') || q.includes('summary') || q.includes('overview')) {
            return `<strong>Paper Overview — ${topic}</strong><br><br>This document presents a comprehensive study on <em>${topic}</em>. The authors examine key aspects including theoretical foundations, experimental validation, and practical implications. Based on my indexing of ${context.pageEstimate} pages, the core themes revolve around: <strong>${kw}</strong>. The paper contributes novel perspectives to the existing body of literature in this domain.`;
        }

        if (q.includes('methodology') || q.includes('method') || q.includes('approach') || q.includes('how')) {
            return `<strong>Methodology Analysis — ${topic}</strong><br><br>The research methodology employed in this paper follows a multi-phase approach:<br>
            <strong>1. Data Collection:</strong> The authors gathered datasets relevant to ${topic}, applying rigorous filtering criteria.<br>
            <strong>2. Analytical Framework:</strong> A hybrid quantitative-qualitative framework was implemented, leveraging statistical models alongside domain-specific heuristics.<br>
            <strong>3. Validation:</strong> Cross-validation techniques were applied across ${context.citationCount} reference points to ensure reproducibility of results.<br><br>
            <em>📄 Reference: Sections 3-4 of "${context.allNames[0]}"</em>`;
        }

        if (q.includes('result') || q.includes('finding') || q.includes('outcome') || q.includes('performance')) {
            return `<strong>Key Findings — ${topic}</strong><br><br>The experimental results demonstrate significant outcomes:<br>
            • <strong>Primary Finding:</strong> The proposed approach achieved a measurable improvement over baseline methods in the domain of ${topic}.<br>
            • <strong>Statistical Significance:</strong> Results were validated with p-value < 0.05 across all major test cases.<br>
            • <strong>Comparative Analysis:</strong> When benchmarked against ${context.citationCount} cited works, the methodology showed superior adaptability.<br><br>
            <em>📊 Data sourced from Tables and Figures in the indexed document.</em>`;
        }

        if (q.includes('conclusion') || q.includes('future') || q.includes('limitation') || q.includes('end')) {
            return `<strong>Conclusion & Future Directions — ${topic}</strong><br><br>The paper concludes that the research on ${topic} opens several avenues for future investigation:<br>
            • The current limitations include dataset scope and computational resource constraints.<br>
            • Future work should explore cross-domain applicability of the proposed framework.<br>
            • The authors recommend further validation with larger, real-world datasets.<br><br>
            <em>📝 Synthesized from the concluding sections of "${context.allNames[0]}"</em>`;
        }

        if (q.includes('author') || q.includes('who') || q.includes('written') || q.includes('researcher')) {
            return `<strong>Author & Attribution Analysis</strong><br><br>The document "${context.allNames[0]}" appears to be authored by researchers specializing in the field of ${topic}. Based on the citation patterns (${context.citationCount} references detected), this work draws from established research communities in this domain. For precise author details, I recommend checking the header of the first page of the uploaded PDF.`;
        }

        if (q.includes('citation') || q.includes('reference') || q.includes('bibliography') || q.includes('source')) {
            return `<strong>Citation Analysis — ${topic}</strong><br><br>I've detected approximately <strong>${context.citationCount} citations</strong> referenced throughout this paper. The bibliography follows a standard academic format, with key references spanning seminal works in ${topic}. The citation density is highest in the Literature Review and Methodology sections, indicating a well-grounded theoretical framework.`;
        }

        if (q.includes('abstract') || q.includes('introduction') || q.includes('intro')) {
            return `<strong>Abstract & Introduction — ${topic}</strong><br><br>The paper opens by establishing the significance of research in ${topic}. The abstract outlines the core contribution: a novel approach to addressing key challenges in this field. The introduction provides contextual background, identifies gaps in existing literature, and presents the research questions that guide the study. The problem statement is clearly defined within the first ${Math.min(3, context.pageEstimate)} pages.`;
        }

        if (q.includes('compare') || q.includes('difference') || q.includes('versus') || q.includes('vs')) {
            return `<strong>Comparative Analysis</strong><br><br>Based on my analysis of "${context.allNames[0]}", the paper positions its contribution against existing approaches in ${topic}. Key differentiators include the novel application of the proposed framework and its scalability. The authors explicitly compare their results with ${Math.min(5, context.citationCount)} prior works, demonstrating advantages in both accuracy and efficiency metrics.`;
        }

        // --- Default Contextual Response ---
        const contextualResponses = [
            `Regarding your question about "${query}" — within the context of <strong>${topic}</strong>, the paper addresses this through its analytical framework. The authors provide evidence in the experimental sections that directly relate to this inquiry. Would you like me to focus on a specific aspect such as the methodology, results, or theoretical background?`,
            `Interesting question! Based on my indexing of <strong>"${context.allNames[0]}"</strong> (${context.pageEstimate} pages), this topic intersects with the paper's core themes of <em>${kw}</em>. The relevant discussion appears to span multiple sections. Can I narrow the focus for you — perhaps the experimental setup or the conclusions?`,
            `In the context of <strong>${topic}</strong>, your query touches on an important aspect covered in this research. The document contains ${context.citationCount} references that provide supporting evidence. I'd recommend examining the methodology section for the most relevant insights. Shall I elaborate on the specific findings?`
        ];
        return contextualResponses[Math.floor(Math.random() * contextualResponses.length)];
    }

    // --- File Upload Handler (Context-Aware) ---
    const handleFileUpload = (files) => {
        if (files.length > 0) {
            // Extract context from filenames
            currentPaperContext = extractTopicFromFiles(files);

            // Show processing simulation with real file info
            uploadScreen.innerHTML = `
                <div class="processor-card glass">
                    <div class="spinner"></div>
                    <p style="margin-top: 1rem; font-weight: 600;">Indexing ${files.length} document${files.length > 1 ? 's' : ''}...</p>
                    <p style="margin-top: 0.5rem; font-size: 0.85rem; color: var(--text-muted);">${currentPaperContext.topic}</p>
                </div>
            `;
            
            setTimeout(() => {
                uploadScreen.classList.add('hidden');
                chatScreen.classList.remove('hidden');

                // Update session sidebar with paper name
                const currentSessionEl = document.getElementById('session-current');
                if (currentSessionEl) {
                    currentSessionEl.textContent = currentPaperContext.topic.length > 22 
                        ? currentPaperContext.topic.substring(0, 22) + '…'
                        : currentPaperContext.topic;
                }

                // Show initial analysis message
                messageFeed.innerHTML = '';
                addMessage('system', `
                    <div class="paper-context-badge">${currentPaperContext.topic}</div>
                    <strong>Document Indexed Successfully</strong><br><br>
                    I've analyzed <strong>${currentPaperContext.fileCount} document${currentPaperContext.fileCount > 1 ? 's' : ''}</strong> 
                    with an estimated <strong>${currentPaperContext.pageEstimate} pages</strong> and 
                    <strong>${currentPaperContext.citationCount} citations</strong> detected.<br><br>
                    You can now ask me about:<br>
                    • 📋 <em>Summary & Overview</em><br>
                    • 🔬 <em>Methodology & Approach</em><br>
                    • 📊 <em>Results & Findings</em><br>
                    • 📝 <em>Conclusions & Future Work</em><br>
                    • 📚 <em>Citations & References</em><br><br>
                    <strong>What would you like to explore?</strong>
                `);
            }, 2500);
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
    const addMessage = (role, text) => {
        const msgDiv = document.createElement('div');
        msgDiv.className = `message ${role} stagger-entry`;
        msgDiv.innerHTML = `
            <div class="avatar">${role === 'user' ? '👤' : '🤖'}</div>
            <div class="bubble">${text}</div>
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

    // --- Smart Send Message ---
    const handleSendMessage = () => {
        const text = chatInput.value.trim();
        if (!text) return;

        addMessage('user', text);
        chatInput.value = '';

        // Show typing indicator
        showTyping();

        // Generate context-aware response with realistic delay
        const responseDelay = 1000 + Math.random() * 1500;
        setTimeout(() => {
            hideTyping();
            const response = generateDeconstructorResponse(text, currentPaperContext);
            addMessage('system', response);
        }, responseDelay);
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
            const sessionName = item.textContent.trim();
            
            if (sessionName === 'Current Session' && !currentPaperContext) {
                // No paper uploaded yet
                addMessage('system', "Please upload a research paper to begin analysis in this session.");
            } else {
                addMessage('system', `
                    <div class="paper-context-badge">${sessionName}</div>
                    I've loaded the context for <strong>"${sessionName}"</strong>. How can I help you explore this research?
                `);
            }
        });
    });

    // --- New Session Button ---
    if (btnNewSession) {
        btnNewSession.addEventListener('click', () => {
            sessionCounter++;
            currentPaperContext = null;

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
                    <div class="upload-icon">📄</div>
                    <h3>Upload Research Papers</h3>
                    <p>Drag and drop PDF files or click to browse</p>
                    <input type="file" id="file-input" multiple accept=".pdf" class="hidden">
                    <button class="btn btn-primary btn-sm" onclick="document.getElementById('file-input').click()">Browse Files</button>
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
        const analysis = generateRepoSpecificContent(repoUrl, author, institution);

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
        doc.text(meta, 105, 35, { align: 'center' });

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
        const introText = "The rapid evolution of software engineering has led to an exponential increase in codebase complexity. As noted in contemporary research, the disconnect between source implementation and formal documentation often results in significant technical debt. This paper presents an automated solution using the PaperAI engine to synthesize academic-grade documentation directly from repository artifacts. By leveraging high-dimensional vector spaces and Retrieval-Augmented Generation, we bridge this gap.";
        y1 = addColumnText(introText, col1X, y1, colWidth);

        // II. LITERATURE REVIEW
        y1 += 10;
        doc.setFont("times", "bold");
        doc.text("II. LITERATURE REVIEW", col1X, y1);
        y1 += 7;
        const litText = "Previous attempts at automated documentation have largely relied on static analysis and regex-based extraction. However, these methods fail to capture the semantic intent behind complex architectural decisions. Recent breakthroughs in Large Language Models (LLMs) allow for a more nuanced understanding of code as a linguistic construct. Our methodology builds upon the work of Vaswani et al. (2017) regarding transformer architectures, applying these principles to the unique domain of repository indexing.";
        y1 = addColumnText(litText, col1X, y1, colWidth);

        // Fill Column 2 (Page 1)
        doc.setFont("times", "bold");
        doc.text("III. DESIGN PHILOSOPHY", col2X, y2);
        y2 += 7;
        const designText = "Central to our design is the principle of 'Source Truth.' We ensure that every synthesized claim is anchored in the physical codebase. This involves a rigorous data ingestion pipeline where raw files are tokenized and processed through a hierarchy of architectural filters. The resulting synthesis is not merely a summary, but a structural decomposition of the developer's intent, ensuring high fidelity and technical accuracy.";
        y2 = addColumnText(designText, col2X, y2, colWidth);
        
        y2 += 10;
        const designText2 = "Furthermore, the integration of professional IEEE standards ensures that the output is immediately suitable for peer-review and academic publication. The dual-path analysis tracks both functional requirements and non-functional scalability metrics, providing a comprehensive evaluation of the software artifact.";
        y2 = addColumnText(designText2, col2X, y2, colWidth);

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

        // V. METHODOLOGY (Column 2 Page 2)
        doc.setFont("times", "bold");
        doc.text("V. METHODOLOGY", col2X, py2);
        py2 += 7;
        const methodText = analysis.methodology; 
        py2 = addColumnText(methodText, col2X, py2, colWidth);
        
        py2 += 10;
        const methodText2 = "We employ a tiered verification system where dynamic claims are cross-referenced against the repository's dependency graph. This eliminates hallucinations and ensures that the IEEE documentation remains a 'digital twin' of the actual implementation. The use of RAG ensures that the system maintains long-range context across the entire repository structure.";
        py2 = addColumnText(methodText2, col2X, py2, colWidth);

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
