/* arXiv / DOI import + citation graph (Worker B, #25). XSS-safe: textContent only. */
(function () {
    'use strict';

    /* Integrator-provided endpoints. PDF_IMPORT_URL is the contract from the brief;
       the others mirror the expected route list. */
    const PDF_IMPORT_URL = "/api/study/import/pdf";
    const IMPORT_ARXIV_URL = "/api/study/import/arxiv";
    const IMPORT_DOI_URL = "/api/study/import/doi";
    const SEARCH_URL = "/api/study/import/search";
    const FULL_IMPORT_URL = "/api/study/import/full";
    const DELETE_URL_FOR = function (paperId) {
        return "/api/study/import/" + encodeURIComponent(paperId);
    };
    const QUEUE_URL = "/api/study/import/queue";
    const CITATIONS_URL = "/api/study/import/citations";
    const COLLECTIONS_URL = "/api/study/collections";
    const COLLECTION_PAPERS_URL_FOR = function (token) {
        return "/api/study/collections/" + encodeURIComponent(token) + "/papers";
    };
    const DNA_URL_FOR = function (docId) {
        return "/study/api/documents/" + encodeURIComponent(docId) + "/dna";
    };
    const GRAPH_URL_FOR = function (openalexId) {
        return "/api/study/import/" + encodeURIComponent(openalexId) + "/graph";
    };
    const CITE_URL_BASE = "/research/cite";

    var state = { papers: [], graph: null, zoom: 1 };

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

    function showStatus(msg, kind) {
        var box = document.getElementById('si-status');
        box.textContent = msg;
        box.className = 'si-status show' + (kind === 'ok' ? ' ok' : '');
    }

    function hideStatus() {
        document.getElementById('si-status').className = 'si-status';
    }

    function setMsg(id, msg, kind) {
        var box = document.getElementById(id);
        box.textContent = msg;
        box.className = 'si-msg ' + (kind || '');
    }

    function clearMsg(id) {
        var box = document.getElementById(id);
        box.textContent = '';
        box.className = 'si-msg';
    }

    function normArxivId(s) {
        s = (s || '').trim()
            .replace(/^https?:\/\/(export\.)?arxiv\.org\/(abs|pdf)\//i, '')
            .replace(/^arxiv:/i, '').replace(/\.pdf$/i, '').replace(/v\d+$/, '');
        return s;
    }

    function normDoi(s) {
        s = (s || '').trim()
            .replace(/^https?:\/\/(dx\.)?doi\.org\//i, '')
            .replace(/^doi:\s*/i, '').replace(/\/$/, '');
        return s;
    }

    var ARXIV_NEW_RE = /^\d{2}(0[1-9]|1[0-2])\.\d{4,5}$/;
    var ARXIV_OLD_RE = /^[a-z\-]+(\.[A-Z]{2})?\/\d{2}(0[1-9]|1[0-2])\d{3}$/;
    var DOI_RE = /^10\.\d{4,9}\/[-._;()/:A-Z0-9]+$/i;

    /* ------------------------------------------------------------ import */
    function importPaper(kind, rawValue, msgId) {
        hideStatus();
        clearMsg(msgId);
        var value = rawValue.trim();
        if (!value) {
            setMsg(msgId, 'Please enter a value first.', 'err');
            return;
        }
        var norm = kind === 'arxiv' ? normArxivId(value) : normDoi(value);
        var valid = kind === 'arxiv'
            ? (ARXIV_NEW_RE.test(norm) || ARXIV_OLD_RE.test(norm))
            : DOI_RE.test(norm);
        if (!valid) {
            setMsg(msgId, kind === 'arxiv'
                ? 'That does not look like a valid arXiv ID (expected YYMM.NNNNN, e.g. 1706.03762).'
                : 'That does not look like a valid DOI (expected 10.xxxx/…, e.g. 10.1038/nature12373).', 'err');
            return;
        }
        var url = kind === 'arxiv' ? IMPORT_ARXIV_URL : IMPORT_DOI_URL;
        var body = kind === 'arxiv' ? { arxiv_id: norm } : { doi: norm };
        setMsg(msgId, 'Fetching…', 'ok');
        fetch(url, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(body),
        })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                if (data && data.error) {
                    setMsg(msgId, data.error, 'err');
                    return;
                }
                if (data.deduped) {
                    setMsg(msgId, 'Already in your imports: ' + (data.title || 'untitled'), 'ok');
                } else {
                    setMsg(msgId, 'Imported: ' + (data.title || 'untitled'), 'ok');
                }
                addPaper(data);
            })
            .catch(function () {
                setMsg(msgId, 'Service unavailable — please try again.', 'err');
                showStatus('Import service unavailable — please try again.');
            });
    }

    function addPaper(paper) {
        if (!paper) return;
        /* Normalize: single-import responses carry import_id, not id. */
        if (!paper.id && paper.import_id) paper.id = paper.import_id;
        /* Never show the same paper twice (deduped imports, re-imports). */
        var dup = state.papers.some(function (p) {
            if (paper.id && p.id) return String(p.id) === String(paper.id);
            if (paper.arxiv_id && p.arxiv_id) return p.arxiv_id === paper.arxiv_id;
            if (paper.doi && p.doi) return p.doi === paper.doi;
            return false;
        });
        if (dup) { renderResults(); return; }
        /* Newest first, matching the server list order. */
        state.papers.unshift(paper);
        renderResults();
    }

    function shortAuthors(paper) {
        var a = paper.authors || [];
        if (!a.length) return 'Unknown authors';
        return a.slice(0, 4).join(', ') + (a.length > 4 ? ' et al.' : '');
    }

    function trunc(s, n) {
        s = s || '';
        return s.length > n ? s.slice(0, n) + '…' : s;
    }

    function filteredPapers() {
        var q = '';
        var input = document.getElementById('import-filter');
        if (input) q = (input.value || '').trim().toLowerCase();
        if (!q) return state.papers;
        return state.papers.filter(function (p) {
            var hay = ((p.title || '') + ' ' + (p.authors || []).join(' ')).toLowerCase();
            return hay.indexOf(q) !== -1;
        });
    }

    function renderResults() {
        var wrap = document.getElementById('results');
        wrap.innerHTML = '';
        var list = filteredPapers();
        if (!list.length) {
            var empty = el('div', 'b-card si-empty',
                state.papers.length
                    ? 'No imported papers match this filter.'
                    : 'No papers imported yet — try an arXiv ID or DOI above.');
            wrap.appendChild(empty);
            return;
        }
        list.forEach(function (paper, idx) {
            wrap.appendChild(renderPaperCard(paper, idx));
        });
    }

    function metaBitsFor(paper) {
        var bits = [];
        if (paper.year) bits.push(String(paper.year));
        if (paper.venue) bits.push(paper.venue);
        if (paper.sources && paper.sources.length) {
            bits.push('via ' + paper.sources.join(' + '));
        } else if (paper.source) {
            bits.push('via ' + paper.source);
        }
        if (paper.oa_version) bits.push('version: ' + paper.oa_version);
        return bits;
    }

    function renderPaperCard(paper, idx) {
        var card = el('div', 'b-card b-card--lite si-card');

        if (paper.is_retracted) {
            card.appendChild(el('span', 'si-pill danger', '⚠ Retracted'));
        }
        card.appendChild(el('h3', null, paper.title || 'Untitled'));
        var metaBits = metaBitsFor(paper);
        var meta = el('div', 'si-meta',
            shortAuthors(paper) + (metaBits.length ? ' · ' + metaBits.join(' · ') : ''));
        card.appendChild(meta);
        if (paper.abstract) card.appendChild(el('p', 'si-abs', trunc(paper.abstract, 320)));

        /* Note created before the actions so handlers can close over it. */
        var note = el('div', 'si-msg');
        note.id = 'paper-note-' + idx;

        var actions = el('div', 'si-actions');

        var graphBtn = el('button', 'si-btn', 'View graph');
        graphBtn.type = 'button';
        if (!paper.openalex_id) {
            graphBtn.disabled = true;
            graphBtn.title = 'No OpenAlex record — graph unavailable';
        } else {
            graphBtn.addEventListener('click', function () { loadGraph(paper.openalex_id); });
        }
        actions.appendChild(graphBtn);

        var pdfBtn = el('button', 'si-btn primary', 'Import PDF to library');
        pdfBtn.type = 'button';
        pdfBtn.addEventListener('click', function () { importPdf(paper, pdfBtn, note); });
        actions.appendChild(pdfBtn);

        var fullBtn = el('button', 'si-btn primary', 'Import + library');
        fullBtn.type = 'button';
        if (!paper.arxiv_id && !paper.doi) {
            fullBtn.disabled = true;
            fullBtn.title = 'No ID available for one-click import';
        } else {
            fullBtn.addEventListener('click', function () { fullImport(paper, fullBtn, note); });
        }
        actions.appendChild(fullBtn);

        var cite = el('a', 'si-btn', 'Cite this');
        cite.href = CITE_URL_BASE + '?title=' + encodeURIComponent(paper.title || '') +
            '&authors=' + encodeURIComponent((paper.authors || []).join(', ')) +
            (paper.year ? '&year=' + encodeURIComponent(paper.year) : '') +
            (paper.doi ? '&doi=' + encodeURIComponent(paper.doi) : '');
        actions.appendChild(cite);

        if (paper.id) {
            var delBtn = el('button', 'si-btn', 'Delete');
            delBtn.type = 'button';
            delBtn.addEventListener('click', function () { deletePaper(paper, delBtn); });
            actions.appendChild(delBtn);
        }

        card.appendChild(actions);
        card.appendChild(note);
        return card;
    }

    function importPdf(paper, btn, note) {
        hideStatus();
        btn.disabled = true;
        fetch(PDF_IMPORT_URL, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({
                pdf_url: paper.pdf_url,
                title: paper.title,
                authors: paper.authors,
                year: paper.year,
                doi: paper.doi,
                arxiv_id: paper.arxiv_id,
                openalex_id: paper.openalex_id,
            }),
        })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                btn.disabled = false;
                if (data && data.error) {
                    showStatus('PDF import failed: ' + data.error);
                } else {
                    hideStatus();
                    btn.textContent = 'Added to library ✓';
                    if (data && data.doc && data.doc.id) fetchDnaIntoNote(data.doc.id, note);
                }
            })
            .catch(function () {
                btn.disabled = false;
                showStatus('Import service unavailable — please try again.');
            });
    }

    /* One-click metadata import + library add. The card's paper is already
       listed, so no addPaper() here (a re-render would detach btn/note). */
    function fullImport(paper, btn, note) {
        hideStatus();
        btn.disabled = true;
        var body = {};
        if (paper.arxiv_id) body.arxiv_id = paper.arxiv_id;
        else if (paper.doi) body.doi = paper.doi;
        fetch(FULL_IMPORT_URL, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(body),
        })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                if (data && data.error) {
                    btn.disabled = false;
                    showStatus('Import failed: ' + data.error);
                    return;
                }
                if (data && data.deduped && note) {
                    note.textContent = 'Already in your imports';
                    note.className = 'si-msg ok';
                }
                if (data && data.doc) {
                    btn.textContent = 'In library ✓';
                    showStatus('Imported + added to library', 'ok');
                } else {
                    btn.disabled = false;
                    showStatus('Imported (PDF unavailable: ' +
                        ((data && data.doc_error) || 'no open-access PDF') + ')');
                }
            })
            .catch(function () {
                btn.disabled = false;
                showStatus('Import service unavailable — please try again.');
            });
    }

    function deletePaper(paper, btn) {
        if (!window.confirm('Delete this import from your list?')) return;
        hideStatus();
        btn.disabled = true;
        fetch(DELETE_URL_FOR(paper.id), {
            method: 'DELETE',
            headers: csrfHeaders(),
        })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                if (!data || data.error || data.success === false) {
                    btn.disabled = false;
                    showStatus('Delete failed: ' + ((data && data.error) || 'unknown error'));
                    return;
                }
                state.papers = state.papers.filter(function (p) {
                    return String(p.id) !== String(paper.id);
                });
                renderResults();
            })
            .catch(function () {
                btn.disabled = false;
                showStatus('Delete service unavailable — please try again.');
            });
    }

    /* DNA preview after a library import. Bonus only: any failure is silent. */
    function fetchDnaIntoNote(docId, note) {
        if (!note) return;
        fetch(DNA_URL_FOR(docId), { headers: csrfHeaders() })
            .then(function (resp) { return resp.json().catch(function () { return null; }); })
            .then(function (data) {
                if (!data) return;
                var dna = data.dna || data;
                if (!dna || typeof dna !== 'object') return;
                var fields = [
                    ['Research question', 'research_question'],
                    ['Method', 'method'],
                    ['Dataset', 'dataset'],
                    ['Sample size', 'sample_size'],
                    ['Key result', 'key_result'],
                    ['Limitation', 'limitation'],
                ];
                var lines = [];
                fields.forEach(function (f) {
                    var v = dna[f[1]];
                    if (!v) return;
                    var text = v.text || v.value || (typeof v === 'string' ? v : '');
                    if (!text) return;
                    var line = f[0] + ': ' + text;
                    if (v.page) line += ' (p.' + v.page + ')';
                    lines.push(line);
                });
                if (!lines.length) return;
                note.innerHTML = '';
                note.className = 'si-msg ok';
                lines.forEach(function (line) {
                    note.appendChild(el('div', null, line));
                });
            })
            .catch(function () { /* silent: DNA is a bonus */ });
    }

    /* ------------------------------------------------------------ graph */
    function loadGraph(openalexId) {
        hideStatus();
        var svg = document.getElementById('graph-svg');
        svg.innerHTML = '';
        var loading = el('div', 'si-empty', 'Loading citation graph…');
        document.getElementById('graph-details').innerHTML = '';
        document.getElementById('graph-details').appendChild(loading);

        fetch(GRAPH_URL_FOR(openalexId), { headers: csrfHeaders() })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                document.getElementById('graph-details').innerHTML = '';
                if (data && data.error) {
                    showStatus('Graph unavailable: ' + data.error + ' — please try again.');
                    renderGraphEmpty(svg, data.error);
                    return;
                }
                state.graph = data;
                state.zoom = 1;
                setupGraphYearSlider(data);
                renderGraph(svg, data);
            })
            .catch(function () {
                document.getElementById('graph-details').innerHTML = '';
                showStatus('Citation graph service unavailable — please try again.');
                renderGraphEmpty(svg, 'Service unavailable — please try again.');
            });
    }

    function renderGraphEmpty(svg, msg) {
        svg.innerHTML = '';
        var t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        t.setAttribute('x', '50%');
        t.setAttribute('y', '50%');
        t.setAttribute('text-anchor', 'middle');
        t.setAttribute('fill', '#94a3b8');
        t.textContent = msg;
        svg.appendChild(t);
    }

    function nodeRadius(n) {
        if (n.is_center) return 24;
        var r = 10 + Math.min(10, Math.sqrt(n.cited_by_count || 0));
        return Math.round(r);
    }

    function renderGraph(svg, data) {
        svg.innerHTML = '';
        var W = svg.clientWidth || 900, H = 460;
        var cx = W / 2, cy = H / 2;
        var R = Math.min(W, H) / 2 - 70;

        var g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        g.setAttribute('id', 'graph-g');
        svg.appendChild(g);

        var nodes = data.nodes || [];
        var center = nodes.filter(function (n) { return n.is_center; })[0] || nodes[0];
        var others = nodes.filter(function (n) { return n !== center; });

        var pos = {};
        if (center) pos[center.id] = { x: cx, y: cy };
        others.forEach(function (n, i) {
            var ang = (2 * Math.PI * i) / Math.max(others.length, 1) - Math.PI / 2;
            pos[n.id] = { x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang) };
        });

        (data.edges || []).forEach(function (e) {
            if (!pos[e.from] || !pos[e.to]) return;
            var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', pos[e.from].x); line.setAttribute('y1', pos[e.from].y);
            line.setAttribute('x2', pos[e.to].x); line.setAttribute('y2', pos[e.to].y);
            line.setAttribute('class', 'si-edge');
            g.appendChild(line);
        });

        nodes.forEach(function (n) {
            if (!pos[n.id]) return;
            var grp = document.createElementNS('http://www.w3.org/2000/svg', 'g');
            grp.setAttribute('class', 'si-node' + (n.is_center ? ' center' : ''));
            var c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            c.setAttribute('cx', pos[n.id].x); c.setAttribute('cy', pos[n.id].y);
            c.setAttribute('r', nodeRadius(n));
            c.setAttribute('fill', n.is_center ? 'rgba(34,211,238,.25)' : 'rgba(99,102,241,.25)');
            grp.appendChild(c);
            var label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            label.setAttribute('x', pos[n.id].x);
            label.setAttribute('y', pos[n.id].y + nodeRadius(n) + 14);
            label.setAttribute('text-anchor', 'middle');
            var words = (n.title || 'Untitled').split(' ').slice(0, 4).join(' ');
            label.textContent = words.length > 34 ? words.slice(0, 34) + '…' : words;
            grp.appendChild(label);
            var titleEl = document.createElementNS('http://www.w3.org/2000/svg', 'title');
            titleEl.textContent = n.title || 'Untitled';
            grp.appendChild(titleEl);
            grp.addEventListener('click', function () { showNodeDetails(n, data); });
            g.appendChild(grp);
        });

        applyZoom();
        var hint = document.getElementById('graph-hint');
        if (hint) hint.textContent = nodes.length + ' nodes — click any node to inspect it.';
    }

    function applyZoom() {
        var svg = document.getElementById('graph-svg');
        var g = document.getElementById('graph-g');
        if (!g) return;
        var W = svg.clientWidth || 900, H = 460;
        g.setAttribute('transform',
            'translate(' + W / 2 + ',' + H / 2 + ') scale(' + state.zoom + ')' +
            ' translate(' + (-W / 2) + ',' + (-H / 2) + ')');
    }

    function showNodeDetails(n, data) {
        var box = document.getElementById('graph-details');
        box.innerHTML = '';
        var card = el('div', 'b-card b-card--lite');
        card.appendChild(el('h3', null, n.title || 'Untitled'));
        var bits = [];
        if (n.year) bits.push(String(n.year));
        bits.push((n.cited_by_count || 0) + ' citations');
        card.appendChild(el('div', 'si-meta',
            (n.authors || []).slice(0, 5).join(', ') + (bits.length ? ' · ' + bits.join(' · ') : '')));
        if (n.is_center) {
            card.appendChild(el('p', 'si-abs', 'Center paper — the one you imported.'));
        } else if (data) {
            var center = (data.nodes || []).filter(function (x) { return x.is_center; })[0];
            var citesCenter = (data.edges || []).some(function (e) {
                return e.from === n.id && center && e.to === center.id;
            });
            card.appendChild(el('p', 'si-abs',
                citesCenter ? 'This paper cites the center paper.' : 'The center paper cites this work.'));
        }
        if (n.doi) {
            var imp = el('button', 'si-btn primary', 'Import this paper');
            imp.type = 'button';
            imp.addEventListener('click', function () {
                document.getElementById('doi-input').value = n.doi;
                importPaper('doi', n.doi, 'doi-msg');
                window.scrollTo({ top: 0, behavior: 'smooth' });
            });
            var row = el('div', 'si-actions');
            row.appendChild(imp);
            card.appendChild(row);
        }
        box.appendChild(card);
    }

    /* ------------------------------------------------------------ search */
    function renderSearchCard(result) {
        var card = el('div', 'b-card b-card--lite si-card');
        if (result.is_retracted) {
            card.appendChild(el('span', 'si-pill danger', '⚠ Retracted'));
        }
        card.appendChild(el('h3', null, result.title || 'Untitled'));
        var bits = metaBitsFor(result);
        card.appendChild(el('div', 'si-meta',
            shortAuthors(result) + (bits.length ? ' · ' + bits.join(' · ') : '')));
        if (result.abstract) card.appendChild(el('p', 'si-abs', trunc(result.abstract, 320)));
        if (result.cited_by_count) {
            card.appendChild(el('div', 'si-meta', result.cited_by_count + ' citations'));
        }
        var actions = el('div', 'si-actions');
        var imp = el('button', 'si-btn primary', 'Import');
        imp.type = 'button';
        if (result.arxiv_id) {
            imp.addEventListener('click', function () {
                importPaper('arxiv', result.arxiv_id, 'search-msg');
            });
        } else if (result.doi) {
            imp.addEventListener('click', function () {
                importPaper('doi', result.doi, 'search-msg');
            });
        } else {
            imp.disabled = true;
            imp.title = 'No arXiv ID or DOI available to import';
        }
        actions.appendChild(imp);
        card.appendChild(actions);
        return card;
    }

    function searchPapers() {
        hideStatus();
        var input = document.getElementById('search-input');
        var query = (input.value || '').trim();
        var wrap = document.getElementById('search-results');
        clearMsg('search-msg');
        if (!query) {
            setMsg('search-msg', 'Please enter a search query.', 'err');
            return;
        }
        wrap.innerHTML = '';
        wrap.appendChild(el('div', 'b-card si-empty', 'Searching…'));
        fetch(SEARCH_URL, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ query: query }),
        })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                wrap.innerHTML = '';
                if (data && data.error) {
                    setMsg('search-msg', data.error, 'err');
                    return;
                }
                var results = (data && data.results) || [];
                if (!results.length) {
                    wrap.appendChild(el('div', 'b-card si-empty', 'No results for this query.'));
                    return;
                }
                wrap.appendChild(el('div', 'si-meta',
                    results.length + ' result' + (results.length === 1 ? '' : 's')));
                results.forEach(function (r) { wrap.appendChild(renderSearchCard(r)); });
            })
            .catch(function () {
                wrap.innerHTML = '';
                setMsg('search-msg', 'Search service unavailable — please try again.', 'err');
            });
    }

    /* ------------------------------------------------------------ queue */
    function loadQueue() {
        hideStatus();
        var wrap = document.getElementById('queue-results');
        var minutes = parseInt(document.getElementById('queue-minutes').value, 10);
        if (!minutes || minutes < 1) minutes = 45;
        var interests = (document.getElementById('queue-interests').value || '').trim();
        var body = { minutes_per_paper: minutes };
        if (interests) body.interests = interests;
        wrap.innerHTML = '';
        wrap.appendChild(el('div', 'b-card si-empty', 'Building queue…'));
        fetch(QUEUE_URL, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(body),
        })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                wrap.innerHTML = '';
                if (data && data.error) {
                    showStatus('Queue failed: ' + data.error);
                    return;
                }
                // Backend returns {queue: [...], total_papers,
                // total_minutes, guidance}; tolerate a bare list too.
                var qdata = (data && data.queue) || [];
                var queue = Array.isArray(qdata) ? qdata : (qdata.queue || []);
                if (!queue.length) {
                    wrap.appendChild(el('div', 'b-card si-empty',
                        'No imports to queue yet — import papers first.'));
                    return;
                }
                if (!Array.isArray(qdata) && (qdata.total_papers || qdata.guidance)) {
                    var sumBits = [];
                    if (qdata.total_papers) sumBits.push(qdata.total_papers + ' papers');
                    if (qdata.total_minutes) sumBits.push('~' + qdata.total_minutes + ' min');
                    if (qdata.guidance) sumBits.push(qdata.guidance);
                    wrap.appendChild(el('div', 'si-meta', sumBits.join(' · ')));
                }
                var list = el('ol', 'si-queue');
                queue.forEach(function (item) {
                    var li = document.createElement('li');
                    var paperTitle = item.title || 'Untitled';
                    if (item.paper) {
                        if (typeof item.paper === 'string') paperTitle = item.paper;
                        else if (item.paper.title) paperTitle = item.paper.title;
                    }
                    li.appendChild(el('div', null, paperTitle));
                    var bits = [];
                    if (item.score !== undefined && item.score !== null) bits.push('score: ' + item.score);
                    if (item.priority !== undefined && item.priority !== null) bits.push('priority: ' + item.priority);
                    if (item.reason) bits.push(item.reason);
                    if (item.why) bits.push(item.why);
                    var mins = item.minutes || item.minutes_per_paper || item.estimated_minutes || item.est_minutes;
                    if (mins) bits.push('~' + mins + ' min');
                    if (bits.length) li.appendChild(el('div', 'si-meta', bits.join(' · ')));
                    list.appendChild(li);
                });
                wrap.appendChild(list);
            })
            .catch(function () {
                wrap.innerHTML = '';
                showStatus('Queue service unavailable — please try again.');
            });
    }

    /* ------------------------------------------------------------ citations */
    function importMiniButton(doi) {
        var btn = el('button', 'si-btn', 'Import this paper');
        btn.type = 'button';
        btn.addEventListener('click', function () {
            importPaper('doi', doi, 'citations-msg');
        });
        return btn;
    }

    function loadCitations() {
        hideStatus();
        clearMsg('citations-msg');
        var wrap = document.getElementById('citations-results');
        wrap.innerHTML = '';
        wrap.appendChild(el('div', 'b-card si-empty', 'Checking…'));
        fetch(CITATIONS_URL, { headers: csrfHeaders() })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                wrap.innerHTML = '';
                if (data && data.error) {
                    showStatus('Citation check failed: ' + data.error);
                    return;
                }
                var recent = (data && data.recent) || [];
                var bridges = (data && data.bridges) || [];
                if (!recent.length && !bridges.length) {
                    wrap.appendChild(el('div', 'b-card si-empty',
                        'No new citations found for your recent imports.'));
                    return;
                }
                if (recent.length) {
                    wrap.appendChild(el('h3', null, 'Recently citing your papers'));
                    recent.forEach(function (c) {
                        var card = el('div', 'b-card b-card--lite si-card');
                        card.appendChild(el('h3', null, c.title || 'Untitled'));
                        var bits = [];
                        if (c.year) bits.push(String(c.year));
                        card.appendChild(el('div', 'si-meta',
                            shortAuthors(c) + (bits.length ? ' · ' + bits.join(' · ') : '')));
                        if (c.cited_paper_title) {
                            card.appendChild(el('p', 'si-abs', 'cites: ' + c.cited_paper_title));
                        }
                        if (c.doi) {
                            var actions = el('div', 'si-actions');
                            actions.appendChild(importMiniButton(c.doi));
                            card.appendChild(actions);
                        }
                        wrap.appendChild(card);
                    });
                }
                if (bridges.length) {
                    wrap.appendChild(el('h3', null, 'Bridges (cite 2+ of your papers)'));
                    bridges.forEach(function (b) {
                        var card = el('div', 'b-card b-card--lite si-card');
                        card.appendChild(el('h3', null, b.title || 'Untitled'));
                        var bits = [];
                        if (b.year) bits.push(String(b.year));
                        card.appendChild(el('div', 'si-meta',
                            shortAuthors(b) + (bits.length ? ' · ' + bits.join(' · ') : '')));
                        var cites = b.cites || [];
                        if (cites.length) {
                            card.appendChild(el('p', 'si-abs', 'bridges: ' + cites.join(', ')));
                        }
                        if (b.doi) {
                            var actions = el('div', 'si-actions');
                            actions.appendChild(importMiniButton(b.doi));
                            card.appendChild(actions);
                        }
                        wrap.appendChild(card);
                    });
                }
            })
            .catch(function () {
                wrap.innerHTML = '';
                showStatus('Citation service unavailable — please try again.');
            });
    }

    /* ------------------------------------------------------------ collections */
    function currentImportIds() {
        return state.papers.map(function (p) { return p.id; }).filter(function (id) {
            return id !== undefined && id !== null && id !== '';
        });
    }

    function collectionCard(c) {
        var card = el('div', 'b-card b-card--lite si-card');
        card.appendChild(el('h3', null, c.name || 'Untitled collection'));
        var bits = [];
        if (c.paper_count !== undefined && c.paper_count !== null) {
            bits.push(c.paper_count + ' paper' + (c.paper_count === 1 ? '' : 's'));
        }
        if (bits.length) card.appendChild(el('div', 'si-meta', bits.join(' · ')));
        var url = c.url || (c.token ? '/research/collections/' + c.token : '');
        var actions = el('div', 'si-actions');
        if (url) {
            var link = el('a', 'si-btn', 'Open shared collection');
            link.href = url;
            link.target = '_blank';
            link.rel = 'noopener';
            actions.appendChild(link);
            var copyBtn = el('button', 'si-btn', 'Copy link');
            copyBtn.type = 'button';
            copyBtn.addEventListener('click', function () { copyCollectionLink(url, copyBtn); });
            actions.appendChild(copyBtn);
        }
        if (c.token) {
            var upd = el('button', 'si-btn', 'Update to current list');
            upd.type = 'button';
            upd.addEventListener('click', function () { updateCollection(c.token, upd); });
            actions.appendChild(upd);
        }
        card.appendChild(actions);
        return card;
    }

    function copyCollectionLink(url, btn) {
        function done() {
            var old = btn.textContent;
            btn.textContent = 'Copied ✓';
            setTimeout(function () { btn.textContent = old; }, 2000);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).then(done).catch(function () {
                fallbackCopy(url, done);
            });
        } else {
            fallbackCopy(url, done);
        }
    }

    function fallbackCopy(text, done) {
        try {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            var ok = document.execCommand('copy');
            document.body.removeChild(ta);
            if (ok) { done(); return; }
        } catch (e) { /* fall through */ }
        showStatus('Copy failed — link: ' + text);
    }

    function renderCollections(list) {
        var wrap = document.getElementById('collections-list');
        wrap.innerHTML = '';
        if (!list.length) {
            wrap.appendChild(el('div', 'b-card si-empty',
                'No collections yet — create one from your imports above.'));
            return;
        }
        list.forEach(function (c) { wrap.appendChild(collectionCard(c)); });
    }

    function loadCollections() {
        fetch(COLLECTIONS_URL, { headers: csrfHeaders() })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                if (data && data.error) {
                    showStatus('Collections unavailable: ' + data.error);
                    return;
                }
                renderCollections((data && data.collections) || []);
            })
            .catch(function () {
                showStatus('Collections unavailable — please try again later.');
            });
    }

    function createCollection() {
        hideStatus();
        var name = (document.getElementById('collection-name').value || '').trim();
        if (!name) {
            showStatus('Please enter a collection name.');
            return;
        }
        var ids = currentImportIds();
        if (!ids.length) {
            showStatus('Import papers first.');
            return;
        }
        fetch(COLLECTIONS_URL, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ name: name, paper_ids: ids }),
        })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                if (data && data.error) {
                    showStatus('Collection creation failed: ' + data.error);
                    return;
                }
                showStatus('Collection created — share link below.', 'ok');
                loadCollections();
            })
            .catch(function () {
                showStatus('Collection service unavailable — please try again.');
            });
    }

    function updateCollection(token, btn) {
        hideStatus();
        var ids = currentImportIds();
        if (!ids.length) {
            showStatus('Import papers first.');
            return;
        }
        btn.disabled = true;
        fetch(COLLECTION_PAPERS_URL_FOR(token), {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ paper_ids: ids }),
        })
            .then(function (resp) { return resp.json().catch(function () { return { error: 'bad response' }; }); })
            .then(function (data) {
                btn.disabled = false;
                if (data && data.error) {
                    showStatus('Collection update failed: ' + data.error);
                    return;
                }
                showStatus('Collection updated to your current list.', 'ok');
                loadCollections();
            })
            .catch(function () {
                btn.disabled = false;
                showStatus('Collection service unavailable — please try again.');
            });
    }

    /* ------------------------------------------------------------ graph year filter */
    function filterGraphData(data, maxYear) {
        var shown = {};
        var nodes = (data.nodes || []).filter(function (n) {
            if (n.is_center) return true;
            if (n.year === undefined || n.year === null || n.year === '') return true;
            var y = Number(n.year);
            if (isNaN(y)) return true;
            return y <= maxYear;
        });
        nodes.forEach(function (n) { shown[n.id] = true; });
        var edges = (data.edges || []).filter(function (e) {
            return shown[e.from] && shown[e.to];
        });
        return { nodes: nodes, edges: edges };
    }

    function setupGraphYearSlider(data) {
        var slider = document.getElementById('graph-year');
        var label = document.getElementById('graph-year-label');
        var years = (data.nodes || []).map(function (n) { return Number(n.year); })
            .filter(function (y) { return !isNaN(y); });
        if (!years.length) {
            slider.disabled = true;
            label.textContent = 'No year data';
            return;
        }
        var min = Math.min.apply(null, years);
        var max = Math.max.apply(null, years);
        slider.min = min;
        slider.max = max;
        slider.value = max;
        slider.disabled = false;
        label.textContent = '≤ ' + max;
    }

    function applyGraphYearFilter() {
        if (!state.graph) return;
        var slider = document.getElementById('graph-year');
        var value = Number(slider.value);
        var svg = document.getElementById('graph-svg');
        var filtered = filterGraphData(state.graph, value);
        renderGraph(svg, filtered);
        var hint = document.getElementById('graph-hint');
        if (hint) {
            hint.textContent = filtered.nodes.length + ' nodes (year ≤ ' + value +
                ') — click any node to inspect it.';
        }
        var label = document.getElementById('graph-year-label');
        if (label) label.textContent = '≤ ' + value;
    }

    function loadInitialPapers() {
        var tag = document.getElementById('import-initial');
        if (!tag) return;
        try {
            var parsed = JSON.parse(tag.textContent || '[]');
            if (Array.isArray(parsed)) state.papers = parsed;
        } catch (e) {
            state.papers = [];
        }
    }

    /* ------------------------------------------------------------ wiring */
    function wire() {
        var arxivBtn = document.getElementById('arxiv-btn');
        var doiBtn = document.getElementById('doi-btn');
        var arxivInput = document.getElementById('arxiv-input');
        var doiInput = document.getElementById('doi-input');

        arxivBtn.addEventListener('click', function () { importPaper('arxiv', arxivInput.value, 'arxiv-msg'); });
        doiBtn.addEventListener('click', function () { importPaper('doi', doiInput.value, 'doi-msg'); });
        arxivInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') importPaper('arxiv', arxivInput.value, 'arxiv-msg');
        });
        doiInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') importPaper('doi', doiInput.value, 'doi-msg');
        });

        document.getElementById('zoom-in').addEventListener('click', function () {
            state.zoom = Math.min(3, state.zoom + 0.25); applyZoom();
        });
        document.getElementById('zoom-out').addEventListener('click', function () {
            state.zoom = Math.max(0.4, state.zoom - 0.25); applyZoom();
        });
        document.getElementById('zoom-reset').addEventListener('click', function () {
            state.zoom = 1; applyZoom();
        });

        var filterInput = document.getElementById('import-filter');
        if (filterInput) filterInput.addEventListener('input', renderResults);

        var searchBtn = document.getElementById('search-btn');
        var searchInput = document.getElementById('search-input');
        if (searchBtn) searchBtn.addEventListener('click', searchPapers);
        if (searchInput) searchInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') searchPapers();
        });

        var queueBtn = document.getElementById('queue-btn');
        if (queueBtn) queueBtn.addEventListener('click', loadQueue);
        var queueInterests = document.getElementById('queue-interests');
        if (queueInterests) queueInterests.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') loadQueue();
        });

        var citationsBtn = document.getElementById('citations-btn');
        if (citationsBtn) citationsBtn.addEventListener('click', loadCitations);

        var collectionCreateBtn = document.getElementById('collection-create-btn');
        var collectionName = document.getElementById('collection-name');
        if (collectionCreateBtn) collectionCreateBtn.addEventListener('click', createCollection);
        if (collectionName) collectionName.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') createCollection();
        });

        var graphYear = document.getElementById('graph-year');
        if (graphYear) graphYear.addEventListener('input', applyGraphYearFilter);

        loadInitialPapers();
        loadCollections();
        renderResults();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }
})();
