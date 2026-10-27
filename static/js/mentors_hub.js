/* Phase 5: mentor matches, Q&A library, mentor dashboard (mentors hub).
   All rendering uses textContent/createElement — never innerHTML with data.
   Honesty: seed mentors always carry the "Illustrative profile" label;
   real mentors carry "Community mentor" + verified mark. Nothing here
   implies a seed is bookable. */
(function () {
    'use strict';

    function csrfHeaders(extra) {
        var headers = extra || {};
        var m = document.querySelector('meta[name="csrf-token"]');
        if (m) headers['X-CSRFToken'] = m.getAttribute('content');
        return headers;
    }
    function get(url) {
        return fetch(url, {headers: csrfHeaders()})
            .then(function (r) { return r.json().then(function (j) { return {status: r.status, body: j}; }); });
    }
    function post(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: csrfHeaders({'Content-Type': 'application/json'}),
            body: JSON.stringify(body || {})
        }).then(function (r) { return r.json().then(function (j) { return {status: r.status, body: j}; }); });
    }
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }
    function pill(text) { return el('span', 'b-pill', text); }
    function hide(n) { if (n) n.style.display = 'none'; }
    function show(n) { if (n) n.style.display = ''; }

    /* ---------- Seed / real labels ---------- */
    function labelFor(m) {
        if (m.illustrative || m.is_seed) return pill('Illustrative profile');
        if (m.verified) return pill('\u2713 Verified · Community mentor');
        return pill('Community mentor');
    }
    function matchPct(score) {
        if (typeof score !== 'number') return null;
        return Math.round(score <= 1 ? score * 100 : score) + '% match';
    }

    /* ---------- Matched mentors ---------- */
    var matchList = document.getElementById('mn-match-list');
    var matchEmpty = document.getElementById('mn-match-empty');
    if (matchList) {
        get('/api/mentors/matches').then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success) { hide(matchList); return; }
            // Polish: 0% entries are not matches — hide the noisy tail.
            // Scores stay truthful in the API; only the display filters.
            var matches = (res.body.matches || []).filter(function (m) {
                return typeof m.match_score === 'number' && m.match_score > 0;
            });
            if (!matches.length) {
                matchList.textContent = '';
                if (matchEmpty) show(matchEmpty);
                return;
            }
            if (matchEmpty) hide(matchEmpty);
            matches.forEach(function (m) {
                var card = el('article', 'b-card b-reveal');
                var pct = matchPct(m.match_score);
                if (pct) card.appendChild(pill(pct));
                var h = el('h3', null, m.name || '');
                h.style.margin = '.6rem 0 .2rem';
                card.appendChild(h);
                card.appendChild(labelFor(m));
                card.appendChild(el('p', null, m.headline || ''));
                var meta = el('p', null, m.path || '');
                meta.style.color = 'var(--b-muted)';
                meta.style.fontSize = '.86rem';
                card.appendChild(meta);
                (m.reasons || []).forEach(function (reason) {
                    var r = el('p', null, reason);
                    r.style.fontSize = '.88rem';
                    r.style.margin = '.2rem 0';
                    card.appendChild(r);
                });
                matchList.appendChild(card);
            });
        }).catch(function () { hide(matchList); });
    }

    /* ---------- Q&A library ---------- */
    var qaList = document.getElementById('mn-qa-list');
    var qaTopic = document.getElementById('mn-qa-topic');
    var qaTopicsLoaded = false;
    function renderQa(items) {
        if (!qaList) return;
        qaList.textContent = '';
        if (!items.length) {
            qaList.appendChild(el('p', null, 'No questions in this topic yet.'));
            return;
        }
        items.forEach(function (q) {
            var card = el('article', 'b-card b-reveal');
            card.appendChild(el('h3', null, q.question || ''));
            if (q.topic) card.appendChild(pill(q.topic));
            var a = el('p', null, q.answer || '');
            a.style.color = 'var(--b-muted)';
            card.appendChild(a);
            var attr = el('p', null, q.mentor_name ? ('Answered by ' + q.mentor_name) : 'Community knowledge base');
            attr.style.fontSize = '.82rem';
            attr.style.color = 'var(--b-muted)';
            card.appendChild(attr);
            qaList.appendChild(card);
        });
    }
    function loadQa(topic) {
        var url = '/api/mentors/qa';
        if (topic) url += '?topic=' + encodeURIComponent(topic);
        get(url).then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success) return;
            var items = res.body.qa || [];
            renderQa(items);
            if (!qaTopicsLoaded && qaTopic) {
                var seen = {};
                items.forEach(function (q) { if (q.topic) seen[q.topic] = true; });
                Object.keys(seen).sort().forEach(function (t) {
                    var opt = document.createElement('option');
                    opt.value = t;
                    opt.textContent = t;
                    qaTopic.appendChild(opt);
                });
                qaTopicsLoaded = true;
            }
        }).catch(function () {});
    }
    if (qaList) {
        loadQa('');
        if (qaTopic) {
            qaTopic.addEventListener('change', function () { loadQa(qaTopic.value); });
        }
    }

    /* ---------- Ask a question + my questions ---------- */
    var askForm = document.getElementById('mn-qa-ask');
    var askMsg = document.getElementById('mn-qa-ask-msg');
    var mineList = document.getElementById('mn-qa-mine');
    function loadMine() {
        if (!mineList) return;
        get('/api/mentors/qa/mine').then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success) return;
            var items = res.body.questions || [];
            mineList.textContent = '';
            if (!items.length) {
                mineList.appendChild(el('p', null, 'You have not asked anything yet.'));
                return;
            }
            items.forEach(function (q) {
                var row = el('div', 'b-card b-reveal');
                row.appendChild(el('p', null, q.question || q.question_text || ''));
                var st = el('p', null, 'Status: ' + (q.status || 'Sent'));
                st.style.fontSize = '.85rem';
                st.style.color = 'var(--b-muted)';
                row.appendChild(st);
                mineList.appendChild(row);
            });
        }).catch(function () {});
    }
    if (askForm) {
        askForm.addEventListener('submit', function (e) {
            e.preventDefault();
            var q = document.getElementById('mn-qa-question');
            var text = q ? q.value.trim() : '';
            if (!text) {
                if (askMsg) { askMsg.textContent = 'Write a question first.'; askMsg.style.display = 'block'; }
                return;
            }
            post('/api/mentors/qa/ask', {question: text}).then(function (res) {
                if ((res.status === 200 || res.status === 201) && res.body && res.body.success) {
                    if (askMsg) { askMsg.textContent = 'Question sent to the mentor inbox'; askMsg.style.display = 'block'; }
                    if (q) q.value = '';
                    loadMine();
                } else if (askMsg) {
                    askMsg.textContent = (res.body && res.body.error) || 'Could not send your question.';
                    askMsg.style.display = 'block';
                }
            }).catch(function () {
                if (askMsg) { askMsg.textContent = 'Network error. Please try again.'; askMsg.style.display = 'block'; }
            });
        });
        loadMine();
    }

    /* ---------- Mentor dashboard (mentors only; 404 hides silently) ---------- */
    var dash = document.getElementById('mn-dashboard');
    if (dash) {
        get('/api/mentors/dashboard').then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success || !res.body.dashboard) { hide(dash); return; }
            var d = res.body.dashboard;
            show(dash);
            var count = document.getElementById('mn-dash-count');
            if (count) count.textContent = String(d.bookings_count != null ? d.bookings_count : (d.bookings || []).length);
            var bl = document.getElementById('mn-dash-bookings');
            if (bl) {
                bl.textContent = '';
                (d.bookings || []).forEach(function (b) {
                    var row = el('p', null, (b.topic || b.session_topic || 'Session') +
                        (b.status ? ' · ' + b.status : '') +
                        (b.scheduled_display ? ' · ' + b.scheduled_display : (b.scheduled_at ? ' · ' + b.scheduled_at : '')));
                    row.style.fontSize = '.9rem';
                    bl.appendChild(row);
                });
                if (!(d.bookings || []).length) bl.appendChild(el('p', null, 'No bookings yet.'));
            }
            var rl = document.getElementById('mn-dash-reviews');
            if (rl) {
                rl.textContent = '';
                var avg = document.getElementById('mn-dash-avg');
                if (avg) {
                    avg.textContent = (d.average_rating != null && (d.reviews || []).length)
                        ? ('Average rating: ' + d.average_rating + ' (' + d.reviews.length + ' reviews)')
                        : 'No reviews yet';
                }
                (d.reviews || []).forEach(function (r) {
                    var row = el('p', null, (r.rating != null ? (r.rating + '/5 — ') : '') + (r.text || r.review_text || ''));
                    row.style.fontSize = '.9rem';
                    rl.appendChild(row);
                });
            }
            var ql = document.getElementById('mn-dash-qa');
            if (ql) {
                ql.textContent = '';
                (d.qa_questions || []).forEach(function (q) {
                    var row = el('p', null, (q.question || q.question_text || '') +
                        (q.status ? ' · ' + q.status : ''));
                    row.style.fontSize = '.9rem';
                    ql.appendChild(row);
                });
                if (!(d.qa_questions || []).length) ql.appendChild(el('p', null, 'No open questions.'));
            }
        }).catch(function () { hide(dash); });
    }
})();
