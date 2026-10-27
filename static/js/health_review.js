/* Phase 5: weekly pulse, action plans, trends, benchmarks, goals,
   mentor sharing (career health review page).
   All rendering uses textContent/createElement — never innerHTML with data.
   Honesty: missing scores render as "—", never 0; benchmarks always show
   their sample size and say "Not enough data yet" when the median is null. */
(function () {
    'use strict';

    var DIMS = [
        {id: 'direction', title: 'Direction'},
        {id: 'proof', title: 'Proof'},
        {id: 'opportunity', title: 'Opportunity activity'},
        {id: 'preparation', title: 'Preparation'},
        {id: 'wellbeing', title: 'Wellbeing'}
    ];
    function dimTitle(id) {
        for (var i = 0; i < DIMS.length; i++) {
            if (DIMS[i].id === id) return DIMS[i].title;
        }
        return id;
    }

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
    function patch(url, body) {
        return fetch(url, {
            method: 'PATCH',
            headers: csrfHeaders({'Content-Type': 'application/json'}),
            body: JSON.stringify(body || {})
        }).then(function (r) { return r.json().then(function (j) { return {status: r.status, body: j}; }); });
    }
    function del(url) {
        return fetch(url, {
            method: 'DELETE',
            headers: csrfHeaders()
        }).then(function (r) { return r.json().then(function (j) { return {status: r.status, body: j}; }); });
    }
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }
    function hide(n) { if (n) n.style.display = 'none'; }
    function show(n) { if (n) n.style.display = ''; }

    /* ---------- Weekly pulse ---------- */
    var pulseForm = document.getElementById('hr-pulse-form');
    var pulseMsg = document.getElementById('hr-pulse-msg');
    var pulseSaved = document.getElementById('hr-pulse-saved');
    var pulseHistory = document.getElementById('hr-pulse-history');
    function renderPulse(pulse) {
        if (!pulseSaved) return;
        if (!pulse || !pulse.scores) {
            pulseSaved.textContent = 'No pulse recorded this week yet.';
            return;
        }
        var parts = DIMS.map(function (d) {
            var v = pulse.scores[d.id];
            return d.title + ': ' + (v != null ? v : '—');
        });
        pulseSaved.textContent = 'Week ' + (pulse.week_key || '') + ' — ' + parts.join(' · ');
    }
    function renderPulseHistory(history) {
        if (!pulseHistory) return;
        pulseHistory.textContent = '';
        if (!history || !history.length) {
            pulseHistory.appendChild(el('p', null, 'No past pulses yet.'));
            return;
        }
        history.forEach(function (p) {
            var parts = DIMS.map(function (d) {
                var v = p.scores ? p.scores[d.id] : null;
                return d.title + ' ' + (v != null ? v : '—');
            });
            var row = el('p', null, (p.week_key || '') + ' — ' + parts.join(' · '));
            row.style.fontSize = '.88rem';
            pulseHistory.appendChild(row);
        });
    }
    function loadPulse() {
        get('/api/health/pulse').then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success) return;
            renderPulse(res.body.pulse);
            renderPulseHistory(res.body.history);
        }).catch(function () {});
    }
    if (pulseForm) {
        // Prefill from the current pulse if one exists.
        get('/api/health/pulse').then(function (res) {
            if (res.status === 200 && res.body && res.body.success && res.body.pulse && res.body.pulse.scores) {
                DIMS.forEach(function (d) {
                    var sel = document.getElementById('hr-pulse-' + d.id);
                    if (sel && res.body.pulse.scores[d.id] != null) sel.value = String(res.body.pulse.scores[d.id]);
                });
            }
        }).catch(function () {});
        pulseForm.addEventListener('submit', function (e) {
            e.preventDefault();
            var scores = {};
            var missing = [];
            DIMS.forEach(function (d) {
                var sel = document.getElementById('hr-pulse-' + d.id);
                var v = sel ? parseInt(sel.value, 10) : NaN;
                if (isNaN(v) || v < 1 || v > 5) missing.push(d.title);
                else scores[d.id] = v;
            });
            if (missing.length) {
                if (pulseMsg) { pulseMsg.textContent = 'Pick a score from 1 to 5 for: ' + missing.join(', ') + '.'; pulseMsg.style.display = 'block'; }
                return;
            }
            post('/api/health/pulse', {scores: scores}).then(function (res) {
                if (res.status === 200 && res.body && res.body.success) {
                    if (pulseMsg) { pulseMsg.textContent = 'Pulse saved for this week.'; pulseMsg.style.display = 'block'; }
                    loadPulse();
                } else if (pulseMsg) {
                    pulseMsg.textContent = (res.body && res.body.error) || 'Could not save your pulse.';
                    pulseMsg.style.display = 'block';
                }
            }).catch(function () {
                if (pulseMsg) { pulseMsg.textContent = 'Network error. Please try again.'; pulseMsg.style.display = 'block'; }
            });
        });
        loadPulse();
    }

    /* ---------- Action plans ---------- */
    var plansEl = document.getElementById('hr-plans');
    if (plansEl) {
        get('/api/health/action-plans').then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success) { hide(plansEl); return; }
            var plans = res.body.plans || [];
            plansEl.textContent = '';
            if (!plans.length) { hide(plansEl); return; }
            plans.forEach(function (p) {
                var card = el('article', 'b-card b-reveal');
                card.appendChild(el('h3', null, p.label || dimTitle(p.dimension)));
                var scoreLine = 'Current score: ' + (p.current_score != null ? p.current_score : '—');
                var src = p.score_source || p.source;
                if (src) scoreLine += ' (' + src + ')';
                var sl = el('p', null, scoreLine);
                sl.style.color = 'var(--b-muted)';
                sl.style.fontSize = '.88rem';
                card.appendChild(sl);
                if (p.trend != null) {
                    var tr = el('p', null, typeof p.trend === 'number'
                        ? (p.trend > 0 ? '▲ ' + p.trend : (p.trend < 0 ? '▼ ' + Math.abs(p.trend) : '— 0'))
                        : String(p.trend));
                    tr.style.fontSize = '.85rem';
                    card.appendChild(tr);
                }
                (p.actions || []).forEach(function (a) {
                    var link = document.createElement('a');
                    link.className = 'b-btn b-btn--sm';
                    link.href = a.url || '#';
                    link.textContent = a.title || a.feature || 'Open';
                    var row = el('div', 'b-cta-row');
                    row.appendChild(link);
                    card.appendChild(row);
                });
                if (p.goals && p.goals.length) {
                    var g = el('p', null, 'Linked goals: ' + p.goals.join(', '));
                    g.style.fontSize = '.85rem';
                    g.style.color = 'var(--b-muted)';
                    card.appendChild(g);
                }
                plansEl.appendChild(card);
            });
        }).catch(function () { hide(plansEl); });
    }

    /* ---------- Trends ---------- */
    var trendsTable = document.getElementById('hr-trends-table');
    var trendsPulses = document.getElementById('hr-trends-pulses');
    if (trendsTable || trendsPulses) {
        get('/api/health/trends').then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success || !res.body.trends) return;
            var t = res.body.trends;
            if (trendsTable && t.months && t.months.length) {
                trendsTable.textContent = '';
                var head = document.createElement('tr');
                head.appendChild(el('th', null, 'Month'));
                DIMS.forEach(function (d) { head.appendChild(el('th', null, d.title)); });
                trendsTable.appendChild(head);
                t.months.forEach(function (m) {
                    var row = document.createElement('tr');
                    row.appendChild(el('td', null, m.month_key || ''));
                    DIMS.forEach(function (d) {
                        var v = m.dimensions ? m.dimensions[d.id] : null;
                        row.appendChild(el('td', null, v != null ? String(v) : '—'));
                    });
                    trendsTable.appendChild(row);
                });
            }
            if (trendsPulses) {
                trendsPulses.textContent = '';
                (t.pulses || []).forEach(function (p) {
                    var parts = DIMS.map(function (d) {
                        var v = p.scores ? p.scores[d.id] : null;
                        return d.title + ' ' + (v != null ? v : '—');
                    });
                    var row = el('p', null, (p.week_key || '') + ' — ' + parts.join(' · '));
                    row.style.fontSize = '.88rem';
                    trendsPulses.appendChild(row);
                });
                if (!(t.pulses || []).length) trendsPulses.appendChild(el('p', null, 'No weekly pulses yet.'));
            }
        }).catch(function () {});
    }

    /* ---------- Benchmarks ---------- */
    var benchEl = document.getElementById('hr-benchmarks');
    if (benchEl) {
        get('/api/health/benchmarks').then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success) { hide(benchEl); return; }
            var items = res.body.benchmarks || [];
            benchEl.textContent = '';
            if (!items.length) { hide(benchEl); return; }
            items.forEach(function (b) {
                var card = el('article', 'b-card b-reveal');
                card.appendChild(el('h3', null, dimTitle(b.dimension)));
                var n = b.sample_size != null ? b.sample_size : 0;
                var line = (b.peer_median != null)
                    ? ('Peer median: ' + b.peer_median + ' (Median of ' + n + ' peers)')
                    : ('Not enough data yet (' + n + ' peers)');
                var p = el('p', null, line);
                p.style.color = 'var(--b-muted)';
                p.style.fontSize = '.9rem';
                card.appendChild(p);
                benchEl.appendChild(card);
            });
        }).catch(function () { hide(benchEl); });
    }

    /* ---------- Goals ---------- */
    var goalForm = document.getElementById('hr-goal-form');
    var goalMsg = document.getElementById('hr-goal-msg');
    var goalList = document.getElementById('hr-goal-list');
    function loadGoals() {
        if (!goalList) return;
        get('/api/health/goals').then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success) return;
            var goals = res.body.goals || [];
            goalList.textContent = '';
            if (!goals.length) {
                goalList.appendChild(el('p', null, 'No goals yet — add your first one above.'));
                return;
            }
            goals.forEach(function (g) {
                var row = el('label', null, null);
                row.style.display = 'flex';
                row.style.gap = '.5rem';
                row.style.alignItems = 'center';
                row.style.fontSize = '.9rem';
                var cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = !!g.done;
                cb.setAttribute('data-goal', String(g.id));
                cb.addEventListener('change', function () {
                    patch('/api/health/goals/' + g.id, {done: cb.checked}).then(function (r2) {
                        if (!(r2.status === 200 && r2.body && r2.body.success) && goalMsg) {
                            goalMsg.textContent = (r2.body && r2.body.error) || 'Could not update the goal.';
                            goalMsg.style.display = 'block';
                            cb.checked = !cb.checked;
                        }
                    }).catch(function () { cb.checked = !cb.checked; });
                });
                row.appendChild(cb);
                row.appendChild(document.createTextNode(
                    (g.title || '') +
                    (g.dimension ? ' · ' + dimTitle(g.dimension) : '') +
                    (g.target_date ? ' · due ' + g.target_date : '')));
                goalList.appendChild(row);
            });
        }).catch(function () {});
    }
    if (goalForm) {
        goalForm.addEventListener('submit', function (e) {
            e.preventDefault();
            var title = document.getElementById('hr-goal-title');
            var dim = document.getElementById('hr-goal-dimension');
            var date = document.getElementById('hr-goal-date');
            var body = {title: title ? title.value.trim() : ''};
            if (dim && dim.value) body.dimension = dim.value;
            if (date && date.value) body.target_date = date.value;
            if (!body.title) {
                if (goalMsg) { goalMsg.textContent = 'Give your goal a title first.'; goalMsg.style.display = 'block'; }
                return;
            }
            post('/api/health/goals', body).then(function (res) {
                if ((res.status === 200 || res.status === 201) && res.body && res.body.success) {
                    if (title) title.value = '';
                    if (goalMsg) { goalMsg.textContent = 'Goal added.'; goalMsg.style.display = 'block'; }
                    loadGoals();
                } else if (goalMsg) {
                    goalMsg.textContent = (res.body && res.body.error) || 'Could not add the goal.';
                    goalMsg.style.display = 'block';
                }
            }).catch(function () {
                if (goalMsg) { goalMsg.textContent = 'Network error. Please try again.'; goalMsg.style.display = 'block'; }
            });
        });
        loadGoals();
    }

    /* ---------- Mentor sharing ---------- */
    var shareForm = document.getElementById('hr-share-form');
    var shareMsg = document.getElementById('hr-share-msg');
    var sharedList = document.getElementById('hr-shared-list');
    function loadShared() {
        if (!sharedList) return;
        get('/api/health/shared-with').then(function (res) {
            if (res.status !== 200 || !res.body || !res.body.success) return;
            var items = res.body.shared_with || res.body.shared || [];
            sharedList.textContent = '';
            if (!items.length) {
                sharedList.appendChild(el('p', null, 'Not shared with any mentor yet.'));
                return;
            }
            items.forEach(function (s) {
                var mid = s.mentor_id || s.id;
                var row = el('div', null, null);
                row.style.display = 'flex';
                row.style.gap = '.6rem';
                row.style.alignItems = 'center';
                row.style.fontSize = '.9rem';
                row.appendChild(document.createTextNode(s.mentor_name || s.name || ('Mentor #' + mid)));
                var btn = el('button', 'b-btn b-btn--sm', 'Revoke');
                btn.type = 'button';
                btn.addEventListener('click', function () {
                    del('/api/health/shared-with/' + mid).then(function () { loadShared(); }).catch(function () {});
                });
                row.appendChild(btn);
                sharedList.appendChild(row);
            });
        }).catch(function () {});
    }
    if (shareForm) {
        shareForm.addEventListener('submit', function (e) {
            e.preventDefault();
            var inp = document.getElementById('hr-share-mentor');
            var mid = inp ? inp.value.trim() : '';
            if (!mid) {
                if (shareMsg) { shareMsg.textContent = 'Enter a mentor ID first.'; shareMsg.style.display = 'block'; }
                return;
            }
            post('/api/health/share-with-mentor', {mentor_id: parseInt(mid, 10) || mid}).then(function (res) {
                if (res.status === 200 && res.body && res.body.success) {
                    if (shareMsg) { shareMsg.textContent = 'Shared with your mentor.'; shareMsg.style.display = 'block'; }
                    if (inp) inp.value = '';
                    loadShared();
                } else if (shareMsg) {
                    shareMsg.textContent = (res.body && res.body.error) || 'Could not share.';
                    shareMsg.style.display = 'block';
                }
            }).catch(function () {
                if (shareMsg) { shareMsg.textContent = 'Network error. Please try again.'; shareMsg.style.display = 'block'; }
            });
        });
        loadShared();
    }
})();
