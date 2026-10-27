/* Rejection retrospectives: wizard step saving, starter/template card picks,
   experiment logging on the hub. Wave 5 #29.
   All POSTs carry the CSRF token via the X-CSRFToken header. */
(function () {
    'use strict';

    function csrfHeaders(extra) {
        var headers = extra || {};
        var m = document.querySelector('meta[name="csrf-token"]');
        if (m) headers['X-CSRFToken'] = m.getAttribute('content');
        return headers;
    }
    function $(id) { return document.getElementById(id); }
    function showErr(id, msg) {
        var el = $(id);
        if (!el) return;
        el.textContent = msg;
        el.style.display = 'block';
    }
    function showOk(id, msg) {
        var el = $(id);
        if (!el) return;
        el.textContent = msg || 'Saved.';
        el.style.display = 'block';
    }
    function hideMsg(id) {
        var el = $(id);
        if (el) el.style.display = 'none';
    }
    function post(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(body || {})
        }).then(function (r) {
            return r.json().catch(function () { return { error: 'Bad response' }; });
        });
    }

    var APP_ID = (typeof window.RT_APP_ID === 'number') ? window.RT_APP_ID : null;

    /* ---- wizard step navigation ---- */
    function gotoPane(n) {
        var steps = document.querySelectorAll('.rw-step');
        Array.prototype.forEach.call(steps, function (s) {
            var active = s.getAttribute('data-pane') === String(n);
            s.classList.toggle('rw-step--active', active);
            if (active) s.classList.add('rw-step--done');
        });
        var panes = document.querySelectorAll('.rw-pane');
        Array.prototype.forEach.call(panes, function (p) {
            p.classList.toggle('rw-pane--active',
                p.getAttribute('data-pane-body') === String(n));
        });
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }
    var stepBtns = document.querySelectorAll('.rw-step');
    Array.prototype.forEach.call(stepBtns, function (b) {
        b.addEventListener('click', function () { gotoPane(b.getAttribute('data-pane')); });
    });
    var gotoBtns = document.querySelectorAll('[data-goto]');
    Array.prototype.forEach.call(gotoBtns, function (b) {
        b.addEventListener('click', function () { gotoPane(b.getAttribute('data-goto')); });
    });

    /* ---- category filters ---- */
    function wireFilter(filterId, cardsId) {
        var filter = $(filterId);
        if (!filter) return;
        var btns = filter.querySelectorAll('button[data-cat]');
        var cards = document.querySelectorAll('#' + cardsId + ' .rw-card');
        Array.prototype.forEach.call(btns, function (b) {
            b.addEventListener('click', function () {
                Array.prototype.forEach.call(btns, function (x) { x.classList.remove('rw-cat--on'); });
                b.classList.add('rw-cat--on');
                var cat = b.getAttribute('data-cat');
                Array.prototype.forEach.call(cards, function (c) {
                    c.style.display = (cat === 'all' || c.getAttribute('data-cat') === cat) ? '' : 'none';
                });
            });
        });
    }
    wireFilter('rw-h-filter', 'rw-h-cards');
    wireFilter('rw-t-filter', 'rw-t-cards');

    /* ---- starter cards: tap to append to the hypotheses textarea ---- */
    var hyps = $('rw-hyps');
    var hCards = document.querySelectorAll('#rw-h-cards .rw-card');
    Array.prototype.forEach.call(hCards, function (c) {
        c.addEventListener('click', function () {
            var text = c.getAttribute('data-text') || '';
            if (!text || !hyps) return;
            var cur = hyps.value.trim();
            hyps.value = cur ? (cur + '\n\n' + text) : text;
            c.classList.add('rw-card--sel');
        });
    });

    /* ---- template cards: tap to pre-fill the experiment form ---- */
    var tCards = document.querySelectorAll('#rw-t-cards .rw-card');
    Array.prototype.forEach.call(tCards, function (c) {
        c.addEventListener('click', function () {
            var h = $('rw-e-hyp'), ch = $('rw-e-change'), tg = $('rw-e-target'), du = $('rw-e-dur');
            if (h) h.value = c.getAttribute('data-hypothesis') || '';
            if (ch) ch.value = c.getAttribute('data-change') || '';
            if (tg) tg.value = c.getAttribute('data-target') || '';
            if (du) du.value = c.getAttribute('data-duration') || '';
            Array.prototype.forEach.call(tCards, function (x) { x.classList.remove('rw-card--sel'); });
            c.classList.add('rw-card--sel');
        });
    });

    /* ---- wizard saves ---- */
    function wizardBody(step) {
        var body = { application_id: APP_ID, step: step };
        if (step === 'facts') {
            body.facts = $('rw-facts') ? $('rw-facts').value : '';
            body.timeline = $('rw-timeline') ? $('rw-timeline').value : '';
            body.stage_reached = $('rw-stage') ? $('rw-stage').value : '';
            /* Phase 4: reason taxonomy — free text wins when present. */
            var other = $('rw-reason-other') ? $('rw-reason-other').value.trim() : '';
            var picked = null;
            var radios = document.querySelectorAll('input[name="rw-reason"]:checked');
            if (radios.length) picked = radios[0].value;
            if (other) {
                body.reason = 'other';
                body.reason_other = other;
            } else if (picked) {
                body.reason = picked;
            }
        } else if (step === 'patterns') {
            body.observed_patterns = $('rw-patterns') ? $('rw-patterns').value : '';
        } else if (step === 'hypotheses') {
            body.hypotheses = hyps ? hyps.value : '';
        } else if (step === 'experiment') {
            body.experiment = {
                hypothesis: $('rw-e-hyp') ? $('rw-e-hyp').value : '',
                change: $('rw-e-change') ? $('rw-e-change').value : '',
                target: $('rw-e-target') ? $('rw-e-target').value : '',
                duration: $('rw-e-dur') ? $('rw-e-dur').value : ''
            };
        }
        return body;
    }
    var saveBtns = document.querySelectorAll('[data-save]');
    Array.prototype.forEach.call(saveBtns, function (btn) {
        btn.addEventListener('click', function () {
            var step = btn.getAttribute('data-save');
            hideMsg('rw-err-' + step);
            hideMsg('rw-ok-' + step);
            btn.disabled = true;
            post('/retrospectives/api/save', wizardBody(step))
                .then(function (data) {
                    btn.disabled = false;
                    if (data.error) {
                        showErr('rw-err-' + step, data.error);
                        return;
                    }
                    showOk('rw-ok-' + step, 'Saved.');
                    var paneOrder = { facts: '1', patterns: '2', hypotheses: '3', experiment: '4' };
                    var tab = document.querySelector('.rw-step[data-pane="' + (paneOrder[step] || '1') + '"]');
                    if (tab) tab.classList.add('rw-step--done');
                })
                .catch(function () {
                    btn.disabled = false;
                    showErr('rw-err-' + step, 'Network error — please try again.');
                });
        });
    });

    /* ---- complete retrospective ---- */
    var completeBtn = $('rw-complete');
    if (completeBtn && APP_ID) {
        completeBtn.addEventListener('click', function () {
            hideMsg('rw-err-experiment');
            completeBtn.disabled = true;
            post('/retrospectives/api/complete/' + APP_ID, {})
                .then(function (data) {
                    if (data.error) {
                        completeBtn.disabled = false;
                        showErr('rw-err-experiment', data.error);
                        return;
                    }
                    window.location.href = '/retrospectives';
                })
                .catch(function () {
                    completeBtn.disabled = false;
                    showErr('rw-err-experiment', 'Network error — please try again.');
                });
        });
    }

    /* ---- hub: new experiment form ---- */
    var expForm = $('rt-exp-form');
    if (expForm) {
        expForm.addEventListener('submit', function (ev) {
            ev.preventDefault();
            hideMsg('rt-exp-error');
            var btn = expForm.querySelector('button[type="submit"]');
            if (btn) btn.disabled = true;
            post('/retrospectives/api/experiments', {
                hypothesis: $('rt-hyp') ? $('rt-hyp').value : '',
                change: $('rt-change') ? $('rt-change').value : '',
                target: $('rt-target') ? $('rt-target').value : '',
                duration: $('rt-dur') ? $('rt-dur').value : ''
            }).then(function (data) {
                if (btn) btn.disabled = false;
                if (data.error) {
                    showErr('rt-exp-error', data.error);
                    return;
                }
                window.location.reload();
            }).catch(function () {
                if (btn) btn.disabled = false;
                showErr('rt-exp-error', 'Network error — please try again.');
            });
        });
    }

    /* ---- Phase 4: wizard reason taxonomy ---- */
    var reasonsBox = $('rw-reasons');
    if (reasonsBox) {
        fetch('/api/retrospectives/reasons', { headers: csrfHeaders({}) })
            .then(function (r) { return r.json().catch(function () { return {}; }); })
            .then(function (data) {
                var reasons = (data && data.reasons) || [];
                if (!data || data.error || !reasons.length) return; /* free text still works */
                reasons.forEach(function (reason) {
                    var id = (typeof reason === 'string') ? reason : (reason.id || reason.label);
                    var label = (typeof reason === 'string') ? reason : (reason.label || reason.id);
                    if (!id) return;
                    var wrap = document.createElement('label');
                    wrap.style.cssText = 'display:flex;gap:.5rem;align-items:center;font-size:.88rem;cursor:pointer;';
                    var radio = document.createElement('input');
                    radio.type = 'radio';
                    radio.name = 'rw-reason';
                    radio.value = id;
                    wrap.appendChild(radio);
                    wrap.appendChild(document.createTextNode(label));
                    reasonsBox.appendChild(wrap);
                });
            })
            .catch(function () { /* free-text Other still works */ });
    }

    /* ---- Phase 4: hub trends, outcomes, feedback, sharing, learnings ---- */
    function rtGet(url) {
        return fetch(url, { headers: csrfHeaders({}) })
            .then(function (r) { return r.json().catch(function () { return {}; }); });
    }
    function rtPatch(url, body) {
        return fetch(url, {
            method: 'PATCH',
            headers: csrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(body || {})
        }).then(function (r) {
            return r.json().catch(function () { return { error: 'Bad response' }; });
        });
    }
    function rtRowText(label, count) {
        var p = document.createElement('p');
        p.style.margin = '.3rem 0';
        p.appendChild(document.createTextNode(label + ' — ' + count));
        return p;
    }

    /* 90-day trends */
    var trendsSec = $('rt-trends');
    if (trendsSec) {
        rtGet('/api/retrospectives/trends').then(function (data) {
            var trends = data && data.trends;
            if (!data || data.error || !trends) {
                trendsSec.style.display = 'none';
                return;
            }
            if (!trends.total) {
                var body = $('rt-trends-body');
                if (body) body.style.display = 'none';
                var empty = $('rt-trends-empty');
                if (empty) empty.style.display = 'block';
                return;
            }
            var total = $('rt-trends-total');
            if (total) total.textContent = trends.total + ' retrospectives in the last 90 days.';
            function fill(id, rows, keyLabel, keyCount) {
                var box = $(id);
                if (!box) return;
                (rows || []).forEach(function (row) {
                    box.appendChild(rtRowText(row[keyLabel], row[keyCount]));
                });
            }
            fill('rt-trends-reasons', trends.by_reason, 'reason', 'count');
            fill('rt-trends-stages', trends.by_stage, 'stage', 'count');
            fill('rt-trends-weeks', trends.by_week, 'week', 'count');
        }).catch(function () {
            trendsSec.style.display = 'none';
        });
    }

    /* Experiment outcomes */
    var outcomeBtns = document.querySelectorAll('.rt-outcome-save');
    Array.prototype.forEach.call(outcomeBtns, function (btn) {
        btn.addEventListener('click', function () {
            var id = btn.getAttribute('data-id');
            var row = btn.closest('tr');
            var sel = row ? row.querySelector('.rt-outcome') : null;
            var note = row ? row.querySelector('.rt-outcome-note') : null;
            var errEl = row ? row.querySelector('.rt-outcome-error') : null;
            var labelEl = row ? row.querySelector('.rt-outcome-label') : null;
            if (errEl) errEl.style.display = 'none';
            btn.disabled = true;
            rtPatch('/api/retrospectives/experiments/' + encodeURIComponent(id), {
                outcome: sel ? sel.value : 'pending',
                outcome_note: note ? note.value : ''
            }).then(function (data) {
                btn.disabled = false;
                if (!data || data.error || data.success === false) {
                    if (errEl) {
                        errEl.textContent = (data && data.error) || 'Could not save the outcome.';
                        errEl.style.display = 'block';
                    }
                    return;
                }
                var outcome = (data.experiment && data.experiment.outcome) || (sel ? sel.value : 'pending');
                if (labelEl) labelEl.textContent = outcome;
            }).catch(function () {
                btn.disabled = false;
                if (errEl) { errEl.textContent = 'Network error — please try again.'; errEl.style.display = 'block'; }
            });
        });
    });

    /* Interview feedback: toggle form, submit, render entries */
    function renderFeedback(listEl, entries) {
        if (!listEl) return;
        while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
        (entries || []).forEach(function (fb) {
            var box = document.createElement('div');
            box.style.cssText = 'margin-top:.6rem;padding-top:.6rem;border-top:1px solid rgba(255,255,255,.07);';
            var head = document.createElement('p');
            head.style.margin = '0 0 .3rem';
            var headText = (fb.round || 'Feedback');
            if (fb.self_rating != null) headText += ' · rated ' + fb.self_rating + '/5';
            head.appendChild(document.createTextNode(headText));
            box.appendChild(head);
            var qs = fb.questions_asked;
            if (qs && qs.length) {
                var ul = document.createElement('ul');
                ul.style.margin = '.2rem 0';
                (Array.isArray(qs) ? qs : [qs]).forEach(function (q) {
                    ul.appendChild(document.createElement('li')).textContent = q;
                });
                box.appendChild(ul);
            }
            if (fb.notes) {
                var notes = document.createElement('p');
                notes.style.cssText = 'color:var(--b-muted);font-size:.88rem;margin:.2rem 0 0;';
                notes.textContent = fb.notes;
                box.appendChild(notes);
            }
            listEl.appendChild(box);
        });
    }
    var fbToggles = document.querySelectorAll('.rt-fb-toggle');
    Array.prototype.forEach.call(fbToggles, function (btn) {
        btn.addEventListener('click', function () {
            var form = document.querySelector('.rt-fb-form[data-retro="' + btn.getAttribute('data-retro') + '"]');
            if (!form) return;
            var showing = form.style.display !== 'none';
            form.style.display = showing ? 'none' : 'block';
            btn.textContent = showing ? 'Add interview feedback' : 'Hide feedback form';
        });
    });
    var fbForms = document.querySelectorAll('.rt-fb-form');
    Array.prototype.forEach.call(fbForms, function (form) {
        form.addEventListener('submit', function (ev) {
            ev.preventDefault();
            var retroId = form.getAttribute('data-retro');
            var errEl = form.querySelector('.rt-fb-error');
            if (errEl) errEl.style.display = 'none';
            var round = form.querySelector('.rt-fb-round').value.trim();
            if (!round) {
                if (errEl) { errEl.textContent = 'Add the round first.'; errEl.style.display = 'block'; }
                return;
            }
            var questions = form.querySelector('.rt-fb-questions').value
                .split('\n').map(function (s) { return s.trim(); })
                .filter(function (s) { return s.length > 0; });
            var submit = form.querySelector('button[type="submit"]');
            if (submit) submit.disabled = true;
            post('/api/retrospectives/' + encodeURIComponent(retroId) + '/feedback', {
                round: round,
                questions_asked: questions,
                self_rating: parseInt(form.querySelector('.rt-fb-rating').value, 10),
                notes: form.querySelector('.rt-fb-notes').value
            }).then(function (data) {
                if (submit) submit.disabled = false;
                if (!data || data.error || data.success === false) {
                    if (errEl) { errEl.textContent = (data && data.error) || 'Could not save feedback.'; errEl.style.display = 'block'; }
                    return;
                }
                renderFeedback(
                    document.querySelector('.rt-fb-list[data-retro="' + retroId + '"]'),
                    data.feedback);
                form.reset();
                form.style.display = 'none';
                var toggle = document.querySelector('.rt-fb-toggle[data-retro="' + retroId + '"]');
                if (toggle) toggle.textContent = 'Add interview feedback';
            }).catch(function () {
                if (submit) submit.disabled = false;
                if (errEl) { errEl.textContent = 'Network error — please try again.'; errEl.style.display = 'block'; }
            });
        });
    });

    /* Anonymous sharing toggle */
    var shareToggles = document.querySelectorAll('.rt-share-toggle');
    Array.prototype.forEach.call(shareToggles, function (cb) {
        cb.addEventListener('change', function () {
            var retroId = cb.getAttribute('data-retro');
            // NOTE: the checkbox itself carries data-retro, so closest()
            // on that attribute would return the checkbox (which has no
            // .rt-share-status descendant). Target the card div instead.
            var card = cb.closest('div.b-card');
            var status = card ? card.querySelector('.rt-share-status') : null;
            post('/api/retrospectives/' + encodeURIComponent(retroId) + '/share-anonymous',
                { share: !!cb.checked }).then(function (data) {
                    var shared = data && (data.shared === true || (data.success && cb.checked));
                    if (status) status.textContent = shared ? 'Sharing anonymously' : 'Not shared';
                    if (!data || data.error || data.success === false) {
                        cb.checked = !cb.checked;
                        if (status) status.textContent = cb.checked ? 'Sharing anonymously' : 'Not shared';
                    }
                }).catch(function () {
                    cb.checked = !cb.checked;
                    if (status) status.textContent = cb.checked ? 'Sharing anonymously' : 'Not shared';
                });
        });
    });

    /* Public learnings (aggregates only) */
    var learningsSec = $('rt-learnings');
    if (learningsSec) {
        rtGet('/api/retrospectives/learnings').then(function (data) {
            var learnings = (data && data.learnings) || [];
            var list = $('rt-learnings-list');
            var empty = $('rt-learnings-empty');
            if (!data || data.error || !learnings.length) {
                if (list) list.style.display = 'none';
                if (empty) empty.style.display = 'block';
                return;
            }
            learnings.forEach(function (item) {
                var p = document.createElement('p');
                p.style.margin = '.4rem 0';
                p.appendChild(document.createTextNode(
                    (item.reason || '—') +
                    (item.stage ? ' · reached ' + item.stage : '') +
                    ' — Reported in ' + item.count + ' retrospectives'));
                list.appendChild(p);
            });
        }).catch(function () {
            var list = $('rt-learnings-list');
            var empty = $('rt-learnings-empty');
            if (list) list.style.display = 'none';
            if (empty) empty.style.display = 'block';
        });
    }

    /* ---- hub: advance experiment status ---- */
    var advBtns = document.querySelectorAll('.rt-adv');
    Array.prototype.forEach.call(advBtns, function (btn) {
        btn.addEventListener('click', function () {
            var id = btn.getAttribute('data-id');
            var to = btn.getAttribute('data-to');
            btn.disabled = true;
            var result = '';
            if (to === 'done') {
                result = window.prompt('What was the outcome of this experiment?', '') || '';
            }
            post('/retrospectives/api/experiments/' + encodeURIComponent(id) + '/result',
                { status: to, result: result })
                .then(function (data) {
                    if (data.error) {
                        btn.disabled = false;
                        window.alert(data.error);
                        return;
                    }
                    window.location.reload();
                })
                .catch(function () {
                    btn.disabled = false;
                    window.alert('Network error — please try again.');
                });
        });
    });
})();
