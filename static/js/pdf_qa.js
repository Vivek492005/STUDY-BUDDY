/* Source-grounded PDF Q&A — study library frontend (feature #21).
 * XSS-safe: textContent / createTextNode only, never innerHTML.
 * Routes are wired by the integrator; this module talks to:
 *   POST   /study/api/upload            (FormData: file)
 *   POST   /study/api/ask               (JSON: {doc_id, question})
 *   GET    /study/api/documents/<id>/history
 *   DELETE /study/api/documents/<id>
 */
(function () {
    'use strict';

    var API = {
        upload: '/study/api/upload',
        ask: '/study/api/ask',
        history: function (id) { return '/study/api/documents/' + encodeURIComponent(id) + '/history'; },
        remove: function (id) { return '/study/api/documents/' + encodeURIComponent(id); },
        dna: function (id) { return '/study/api/documents/' + encodeURIComponent(id) + '/dna'; },
        examGenerate: '/study/api/exam/generate',
        examGrade: '/study/api/exam/grade',
    };

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function csrfHeaders(extra) {
        var headers = extra || {};
        var m = document.querySelector('meta[name="csrf-token"]');
        if (m) headers['X-CSRFToken'] = m.getAttribute('content');
        return headers;
    }

    function showError(id, msg) {
        var box = document.getElementById(id);
        box.textContent = msg;
        box.style.display = 'block';
    }

    function hideError(id) {
        document.getElementById(id).style.display = 'none';
    }

    function setStatus(id, text) {
        var pill = document.getElementById(id);
        if (!text) {
            pill.style.display = 'none';
            return;
        }
        pill.textContent = text;
        pill.style.display = '';
    }

    var state = { docId: null, docTitle: '', lastQ: '', lastScope: null };

    /* ---------- document selection ---------- */

    function openDoc(id, title) {
        state.docId = id;
        state.docTitle = title;
        document.getElementById('qa-doc-title').textContent = title;
        var view = document.getElementById('qa-doc-view');
        view.hidden = false;
        document.getElementById('qa-answer-wrap').style.display = 'none';
        document.getElementById('qa-question').value = '';
        hideError('qa-ask-error');
        loadHistory();
        view.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function loadHistory() {
        var list = document.getElementById('qa-history');
        list.textContent = '';
        fetch(API.history(state.docId), { headers: csrfHeaders() })
            .then(function (res) { return res.ok ? res.json() : { history: [] }; })
            .then(function (data) {
                var items = (data && data.history) || [];
                if (!items.length) {
                    var row = el('div', 'b-kv-row');
                    var label = el('span', 'b-kv-label', 'No questions asked about this document yet.');
                    label.style.color = 'var(--b-muted)';
                    row.appendChild(label);
                    list.appendChild(row);
                    return;
                }
                items.forEach(function (h) {
                    var row = el('div', 'b-kv-row');
                    row.appendChild(el('span', 'b-kv-label', h.question));
                    row.appendChild(el('span', 'b-kv-val', h.created || ''));
                    row.title = h.answer || '';
                    list.appendChild(row);
                });
            })
            .catch(function () { /* history is best-effort; the Q&A still works */ });
    }

    /* ---------- asking ---------- */

    // Faithfulness badge text for a grounding report from the API.
    function groundingLabel(g) {
        if (!g) return null;
        if (g.abstained) return 'Grounded · abstained, nothing claimed';
        return 'Grounded ' + g.cited + '/' + g.claims + ' claims cited';
    }

    // Render answer text, turning [doc, p.X] / [p.X] markers into citation chips.
    function renderAnswer(text, citations, grounding) {
        var wrap = document.getElementById('qa-answer');
        wrap.textContent = '';
        var parts = String(text).split(/(\[doc, p\.\d+\]|\[p\.\d+\])/g);
        parts.forEach(function (part) {
            var m = /^\[(?:doc, )?p\.(\d+)\]$/.exec(part);
            if (m) {
                wrap.appendChild(el('span', 'b-pill', 'p.' + m[1] + ' cited'));
            } else {
                wrap.appendChild(document.createTextNode(part));
            }
        });

        var chips = document.getElementById('qa-citations');
        chips.textContent = '';
        (citations || []).forEach(function (c) {
            chips.appendChild(el('span', 'b-pill', c.doc + ' · p.' + c.page));
        });
        var gl = groundingLabel(grounding);
        if (gl) chips.appendChild(el('span', 'b-pill', gl));

        var plist = document.getElementById('qa-passages-list');
        plist.textContent = '';
        var passages = citations || [];
        document.getElementById('qa-passages').style.display = passages.length ? '' : 'none';
        passages.forEach(function (c, i) {
            var card = el('div', 'b-card b-card--lite');
            card.appendChild(el('span', 'b-tag', 'Passage ' + (i + 1) + ' · page ' + c.page));
            card.appendChild(el('p', null, c.excerpt));
            plist.appendChild(card);
        });

        document.getElementById('qa-answer-wrap').style.display = '';
    }

    function ask() {
        if (!state.docId) return;
        var q = document.getElementById('qa-question').value.trim();
        hideError('qa-ask-error');
        if (!q) {
            showError('qa-ask-error', 'Type a question first.');
            return;
        }
        var btn = document.getElementById('qa-ask-btn');
        btn.disabled = true;
        setStatus('qa-ask-status', 'Searching the document…');
        fetch(API.ask, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ doc_id: state.docId, question: q }),
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (result) {
                btn.disabled = false;
                setStatus('qa-ask-status', null);
                if (!result.ok) {
                    showError('qa-ask-error',
                        (result.data && result.data.error) || 'Could not answer that question.');
                    return;
                }
                state.lastQ = q;
                state.lastScope = 'doc';
                renderAnswer(result.data.answer, result.data.citations, result.data.grounding);
                var fbtn = document.getElementById('qa-fluent-btn');
                if (fbtn) {
                    fbtn.style.display =
                        (result.data.grounding && !result.data.grounding.abstained) ? '' : 'none';
                }
                loadHistory();
            })
            .catch(function () {
                btn.disabled = false;
                setStatus('qa-ask-status', null);
                showError('qa-ask-error', 'Network error — please try again.');
            });
    }

    /* ---------- library-wide asking ---------- */

    function renderPerDoc(container, perDoc) {
        container.textContent = '';
        (perDoc || []).forEach(function (entry) {
            var card = el('div', 'b-card b-card--lite');
            card.appendChild(el('span', 'b-tag', entry.doc));
            (entry.claims || []).forEach(function (c) {
                var p = el('p', null, '“' + c.text + '” ');
                p.appendChild(el('span', 'b-pill', 'p.' + c.page + ' cited'));
                card.appendChild(p);
            });
            container.appendChild(card);
        });
    }

    function renderContrasts(container, contrasts) {
        container.textContent = '';
        (contrasts || []).forEach(function (flag) {
            var card = el('div', 'b-card b-card--lite');
            card.appendChild(el('span', 'b-tag', 'Possible contrast · ' + flag.signal));
            card.appendChild(el('p', null, flag.docs.join('  vs  ')));
            (flag.claims || []).forEach(function (c) {
                var p = el('p', null, c.doc + ': “' + c.text + '” ');
                p.appendChild(el('span', 'b-pill', 'p.' + c.page));
                card.appendChild(p);
            });
            card.appendChild(el('p', null, flag.note));
            container.appendChild(card);
        });
    }

    function askLib() {
        var q = document.getElementById('qa-lib-question').value.trim();
        hideError('qa-lib-ask-error');
        if (!q) {
            showError('qa-lib-ask-error', 'Type a question first.');
            return;
        }
        var btn = document.getElementById('qa-lib-ask-btn');
        btn.disabled = true;
        setStatus('qa-lib-ask-status', 'Searching the whole library…');
        fetch(API.ask, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ doc_id: null, question: q }),
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (result) {
                btn.disabled = false;
                setStatus('qa-lib-ask-status', null);
                if (!result.ok) {
                    showError('qa-lib-ask-error',
                        (result.data && result.data.error) || 'Could not answer that question.');
                    return;
                }
                state.lastQ = q;
                state.lastScope = 'lib';
                renderLibAnswer(result.data);
            })
            .catch(function () {
                btn.disabled = false;
                setStatus('qa-lib-ask-status', null);
                showError('qa-lib-ask-error', 'Network error — please try again.');
            });
    }

    function renderLibAnswer(d) {
        var wrap = document.getElementById('qa-lib-answer');
        wrap.textContent = '';
        String(d.answer).split(/(\[doc, p\.\d+\]|\[p\.\d+\])/g).forEach(function (part) {
            var m = /^\[(?:doc, )?p\.(\d+)\]$/.exec(part);
            if (m) {
                wrap.appendChild(el('span', 'b-pill', 'p.' + m[1] + ' cited'));
            } else {
                wrap.appendChild(document.createTextNode(part));
            }
        });
        var chips = document.getElementById('qa-lib-citations');
        chips.textContent = '';
        (d.citations || []).forEach(function (c) {
            chips.appendChild(el('span', 'b-pill', c.doc + ' · p.' + c.page));
        });
        var gl = groundingLabel(d.grounding);
        if (gl) chips.appendChild(el('span', 'b-pill', gl));
        if (d.docs_consulted) {
            chips.appendChild(el('span', 'b-pill', d.docs_consulted + ' document(s) consulted'));
        }
        if (d.fluent) chips.appendChild(el('span', 'b-pill', 'Fluent rewrite · citations kept'));
        renderPerDoc(document.getElementById('qa-lib-per-doc'), d.per_doc);
        renderContrasts(document.getElementById('qa-lib-contrasts'), d.contrasts);
        document.getElementById('qa-lib-answer-wrap').style.display = '';
        var fbtn = document.getElementById('qa-lib-fluent-btn');
        if (fbtn) {
            fbtn.style.display = (d.grounding && !d.grounding.abstained && !d.fluent) ? '' : 'none';
        }
    }

    /* ---------- fluent rewrite ---------- */

    function reaskFluent() {
        if (!state.lastQ) return;
        var isLib = state.lastScope === 'lib';
        var btn = document.getElementById(isLib ? 'qa-lib-fluent-btn' : 'qa-fluent-btn');
        var statusId = isLib ? 'qa-lib-ask-status' : 'qa-ask-status';
        var errorId = isLib ? 'qa-lib-ask-error' : 'qa-ask-error';
        if (btn) btn.disabled = true;
        setStatus(statusId, 'Rewriting fluently…');
        fetch(API.ask, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({
                doc_id: isLib ? null : state.docId,
                question: state.lastQ, fluent: true,
            }),
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (result) {
                if (btn) btn.disabled = false;
                if (!result.ok) {
                    setStatus(statusId, null);
                    showError(errorId,
                        (result.data && result.data.error) || 'Could not rewrite that answer.');
                    return;
                }
                var d = result.data;
                if (isLib) {
                    renderLibAnswer(d);
                } else {
                    renderAnswer(d.answer, d.citations, d.grounding);
                    if (d.fluent) {
                        var chips = document.getElementById('qa-citations');
                        chips.appendChild(el('span', 'b-pill', 'Fluent rewrite · citations kept'));
                    }
                }
                if (d.fluent) {
                    setStatus(statusId, null);
                    if (btn) btn.style.display = 'none';
                } else {
                    setStatus(statusId, 'Fluent rewrite unavailable — showing quoted answer.');
                }
            })
            .catch(function () {
                if (btn) btn.disabled = false;
                setStatus(statusId, null);
                showError(errorId, 'Network error — please try again.');
            });
    }

    /* ---------- mock exams ---------- */

    var examState = { questions: [] };

    function startExam(docId, title) {
        hideError('qa-exam-error');
        document.getElementById('qa-exam-result').style.display = 'none';
        setStatus('qa-exam-status', 'Building your exam…');
        fetch(API.examGenerate, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ doc_id: docId, n: 10 }),
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (result) {
                setStatus('qa-exam-status', null);
                if (!result.ok) {
                    showError('qa-exam-error',
                        (result.data && result.data.error) || 'Could not build an exam.');
                    return;
                }
                var exam = result.data.exam;
                examState.questions = exam.questions || [];
                document.getElementById('qa-exam-title').textContent = exam.title;
                var list = document.getElementById('qa-exam-list');
                list.textContent = '';
                examState.questions.forEach(function (q, i) {
                    var card = el('div', 'b-card b-card--lite');
                    card.appendChild(el('span', 'b-tag',
                        (q.kind === 'mcq' ? 'Multiple choice' :
                            q.kind === 'cloze' ? 'Fill in the blank' : 'Short answer')
                        + ' · ' + (q.doc_title || title) + ' · p.' + q.page));
                    card.appendChild(el('p', null, (i + 1) + '. ' + q.question));
                    if (q.kind === 'mcq') {
                        (q.options || []).forEach(function (opt, oi) {
                            var label = document.createElement('label');
                            label.style.display = 'block';
                            var radio = document.createElement('input');
                            radio.type = 'radio';
                            radio.name = 'exam-' + q.id;
                            radio.value = String(oi);
                            label.appendChild(radio);
                            label.appendChild(document.createTextNode(' ' + opt));
                            card.appendChild(label);
                        });
                    } else if (q.kind === 'cloze') {
                        var input = document.createElement('input');
                        input.type = 'text';
                        input.className = 'b-input';
                        input.setAttribute('data-qid', q.id);
                        input.setAttribute('aria-label', 'Your answer');
                        card.appendChild(input);
                    } else {
                        var note = el('p', null,
                            'Write your answer, then grade to see the model answer.');
                        note.style.color = 'var(--b-muted)';
                        card.appendChild(note);
                    }
                    list.appendChild(card);
                });
                var section = document.getElementById('qa-exam');
                section.style.display = '';
                section.scrollIntoView({ behavior: 'smooth', block: 'start' });
            })
            .catch(function () {
                setStatus('qa-exam-status', null);
                showError('qa-exam-error', 'Network error — please try again.');
            });
    }

    function gradeExam() {
        var answers = {};
        examState.questions.forEach(function (q) {
            if (q.kind === 'mcq') {
                var checked = document.querySelector(
                    'input[name="exam-' + q.id + '"]:checked');
                if (checked) answers[q.id] = parseInt(checked.value, 10);
            } else if (q.kind === 'cloze') {
                var input = document.querySelector(
                    '#qa-exam-list input[data-qid="' + q.id + '"]');
                if (input) answers[q.id] = input.value;
            }
        });
        hideError('qa-exam-error');
        setStatus('qa-exam-status', 'Grading…');
        fetch(API.examGrade, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ questions: examState.questions, answers: answers }),
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (result) {
                setStatus('qa-exam-status', null);
                if (!result.ok) {
                    showError('qa-exam-error',
                        (result.data && result.data.error) || 'Could not grade this exam.');
                    return;
                }
                var g = result.data.grade;
                var box = document.getElementById('qa-exam-result');
                box.textContent = '';
                var score = g.score_pct === null ? 'no auto-gradeable questions' :
                    g.auto_correct + '/' + g.auto_total + ' (' + g.score_pct + '%)';
                box.appendChild(el('h3', null, 'Score: ' + score));
                g.results.forEach(function (r) {
                    var p = el('p', null, '');
                    if (r.kind === 'qa') {
                        p.textContent = 'Self-check · ' + (r.doc_title || '') + ' p.' + r.page +
                            ' — model answer: ' + r.expected;
                    } else if (r.correct) {
                        p.textContent = 'Correct · ' + (r.doc_title || '') + ' p.' + r.page;
                    } else {
                        p.textContent = 'Wrong · ' + (r.doc_title || '') + ' p.' + r.page +
                            ' — correct answer: ' + r.expected;
                    }
                    box.appendChild(p);
                });
                if (g.reread && g.reread.length) {
                    box.appendChild(el('h3', null, 'Re-read these pages:'));
                    g.reread.forEach(function (rr) {
                        box.appendChild(el('p', null, (rr.doc_title || '') + ' · p.' + rr.page));
                    });
                }
                box.style.display = '';
                box.scrollIntoView({ behavior: 'smooth', block: 'start' });
            })
            .catch(function () {
                setStatus('qa-exam-status', null);
                showError('qa-exam-error', 'Network error — please try again.');
            });
    }

    /* ---------- Paper DNA ---------- */

    var DNA_FIELDS = [
        ['Research question', 'research_question'],
        ['Method', 'method'],
        ['Dataset', 'dataset'],
        ['Sample size', 'sample_size'],
        ['Key result', 'key_result'],
        ['Limitation', 'limitation'],
    ];

    function toggleDna(btn) {
        var card = btn.closest('article');
        var panel = card && card.querySelector('.qa-dna-panel');
        if (!panel) return;
        if (panel.style.display !== 'none' && panel.textContent) {
            panel.style.display = 'none';
            return;
        }
        panel.textContent = 'Loading Paper DNA…';
        panel.style.display = '';
        fetch(API.dna(btn.getAttribute('data-doc-id')), { headers: csrfHeaders({}) })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                panel.textContent = '';
                if (!data.success) {
                    panel.appendChild(el('p', null, 'Could not load Paper DNA.'));
                    return;
                }
                var dna = data.dna || {};
                DNA_FIELDS.forEach(function (pair) {
                    var field = dna[pair[1]];
                    var row = el('div', 'b-kv-row');
                    row.appendChild(el('span', 'b-kv-label', pair[0]));
                    if (field && field.text) {
                        var v = el('span', null, field.text + ' ');
                        v.appendChild(el('span', 'b-pill', 'p.' + field.page));
                        row.appendChild(v);
                    } else if (pair[1] === 'sample_size') {
                        row.appendChild(el('span', null, 'Not stated in this document'));
                    } else {
                        row.appendChild(el('span', null, 'Not stated in this document'));
                    }
                    panel.appendChild(row);
                });
                var note = el('p', null,
                    'Extracted from the document text — fields the paper does not state are left blank, never invented.');
                note.style.color = 'var(--b-muted)';
                panel.appendChild(note);
            })
            .catch(function () {
                panel.textContent = 'Network error — please try again.';
            });
    }

    /* ---------- upload ---------- */

    function upload(file) {
        hideError('qa-upload-error');
        if (!file) {
            showError('qa-upload-error', 'Choose a PDF file first.');
            return;
        }
        if (file.size > 25 * 1024 * 1024) {
            showError('qa-upload-error', 'File exceeds the 25 MB limit.');
            return;
        }
        var btn = document.getElementById('qa-upload-btn');
        btn.disabled = true;
        setStatus('qa-upload-status', 'Uploading & indexing…');
        var form = new FormData();
        form.append('file', file, file.name);
        fetch(API.upload, {
            method: 'POST',
            headers: csrfHeaders(),
            body: form,
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (result) {
                btn.disabled = false;
                setStatus('qa-upload-status', null);
                if (!result.ok) {
                    showError('qa-upload-error',
                        (result.data && result.data.error) || 'Upload failed.');
                    return;
                }
                // Server re-renders the library grid; reload to show the new doc.
                window.location.reload();
            })
            .catch(function () {
                btn.disabled = false;
                setStatus('qa-upload-status', null);
                showError('qa-upload-error', 'Network error — please try again.');
            });
    }

    /* ---------- delete ---------- */

    function removeDoc(id, title) {
        if (!window.confirm('Delete "' + title + '" and all its questions? This cannot be undone.')) {
            return;
        }
        fetch(API.remove(id), {
            method: 'DELETE',
            headers: csrfHeaders(),
        })
            .then(function (res) { return res.ok; })
            .then(function (ok) {
                if (!ok) {
                    window.alert('Could not delete the document. Please try again.');
                    return;
                }
                var card = document.querySelector('[data-doc-id="' + id + '"]');
                if (card) card.remove();
                if (state.docId === id) {
                    state.docId = null;
                    document.getElementById('qa-doc-view').hidden = true;
                }
                var remaining = document.querySelectorAll('#qa-library [data-doc-id]').length;
                document.getElementById('qa-lib-count').textContent =
                    remaining + ' document' + (remaining === 1 ? '' : 's');
                if (!remaining) window.location.reload();
            })
            .catch(function () {
                window.alert('Network error — please try again.');
            });
    }

    /* ---------- wiring ---------- */

    function init() {
        var dropzone = document.getElementById('qa-dropzone');
        var fileInput = document.getElementById('qa-file');
        if (!dropzone || !fileInput) return;

        dropzone.addEventListener('click', function () { fileInput.click(); });
        dropzone.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                fileInput.click();
            }
        });
        ['dragenter', 'dragover'].forEach(function (evt) {
            dropzone.addEventListener(evt, function (e) {
                e.preventDefault();
                dropzone.style.borderColor = 'var(--b-accent, #22d3ee)';
            });
        });
        ['dragleave', 'drop'].forEach(function (evt) {
            dropzone.addEventListener(evt, function (e) {
                e.preventDefault();
                dropzone.style.borderColor = '';
            });
        });
        dropzone.addEventListener('drop', function (e) {
            var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (f) upload(f);
        });
        fileInput.addEventListener('change', function () {
            if (fileInput.files[0]) {
                document.getElementById('qa-upload-hint').style.display = 'none';
            }
        });
        document.getElementById('qa-upload-btn').addEventListener('click', function () {
            upload(fileInput.files[0]);
        });
        document.getElementById('qa-ask-btn').addEventListener('click', ask);
        document.getElementById('qa-question').addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) ask();
        });
        var libAskBtn = document.getElementById('qa-lib-ask-btn');
        if (libAskBtn) {
            libAskBtn.addEventListener('click', askLib);
            document.getElementById('qa-lib-question').addEventListener('keydown', function (e) {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) askLib();
            });
        }
        var fluentBtn = document.getElementById('qa-fluent-btn');
        if (fluentBtn) fluentBtn.addEventListener('click', reaskFluent);
        var libFluentBtn = document.getElementById('qa-lib-fluent-btn');
        if (libFluentBtn) libFluentBtn.addEventListener('click', reaskFluent);
        var libExamBtn = document.getElementById('qa-lib-exam-btn');
        if (libExamBtn) libExamBtn.addEventListener('click', function () {
            startExam(null, 'Your library');
        });
        document.getElementById('qa-exam-grade-btn').addEventListener('click', gradeExam);

        document.getElementById('qa-library').addEventListener('click', function (e) {
            var openBtn = e.target.closest('.qa-open-doc');
            if (openBtn) {
                openDoc(openBtn.getAttribute('data-doc-id'),
                    openBtn.getAttribute('data-doc-title'));
                return;
            }
            var dnaBtn = e.target.closest('.qa-dna-doc');
            if (dnaBtn) {
                toggleDna(dnaBtn);
                return;
            }
            var examBtn = e.target.closest('.qa-exam-doc');
            if (examBtn) {
                startExam(examBtn.getAttribute('data-doc-id'),
                    examBtn.getAttribute('data-doc-title'));
                return;
            }
            var delBtn = e.target.closest('.qa-delete-doc');
            if (delBtn) {
                removeDoc(delBtn.getAttribute('data-doc-id'),
                    delBtn.getAttribute('data-doc-title'));
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
