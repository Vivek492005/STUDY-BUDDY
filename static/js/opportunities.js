/* Opportunity Radar bookmarks (Wave 1 #9) + Phase 3 matches/alerts/actions.
   XSS-safe: textContent/createElement only, no innerHTML with data. */
(function () {
    'use strict';
    var grid = document.getElementById('oppGrid');
    var tokenMeta = document.querySelector('meta[name="csrf-token"]');

    function headers() {
        var h = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
        if (tokenMeta) h['X-CSRFToken'] = tokenMeta.getAttribute('content');
        return h;
    }

    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }

    function getJSON(url) {
        return fetch(url, { headers: headers() }).then(function (r) {
            return r.json().then(function (b) { return { status: r.status, body: b }; });
        });
    }

    function showInlineMsg(card, text, isErr) {
        var m = card.querySelector('.opp-inline-msg');
        if (!m) return;
        m.textContent = text;
        m.classList.toggle('err', !!isErr);
        m.hidden = false;
    }

    /* ---- refresh status (public; silent on failure) ---- */
    (function refreshStatus() {
        var line = document.getElementById('oppRefreshLine');
        if (!line) return;
        getJSON('/api/opportunities/refresh-status').then(function (res) {
            var b = res.body;
            if (!b || !b.success) return;
            var parts = [];
            if (b.last_refresh) parts.push('Refreshed ' + b.last_refresh);
            else if (b.last_updated) parts.push('Refreshed ' + b.last_updated);
            if (typeof b.open === 'number') parts.push(b.open + ' open');
            if (typeof b.closed === 'number') parts.push(b.closed + ' closed');
            if (!parts.length) return;
            line.textContent = parts.join(' · ');
            line.hidden = false;
        }).catch(function () { /* keep hidden; freshness line stays */ });
    })();

    /* ---- matched for you (authed only; silent on failure) ---- */
    (function matches() {
        var sec = document.getElementById('oppMatches');
        var list = document.getElementById('oppMatchList');
        var empty = document.getElementById('oppMatchEmpty');
        if (!sec || !list) return;
        getJSON('/api/opportunities/matches').then(function (res) {
            var b = res.body;
            if (!b || !b.success || !Array.isArray(b.matches)) return;
            sec.hidden = false;
            if (!b.matches.length) { if (empty) empty.hidden = false; return; }
            b.matches.forEach(function (m) {
                var card = el('div', 'opp-match');
                var a = el('a', null, m.title || m.opp_id);
                a.href = m.url || '#';
                a.target = '_blank';
                a.rel = 'noopener';
                card.appendChild(a);
                if (typeof m.score === 'number') card.appendChild(el('span', 'opp-score', Math.round(m.score) + '% match'));
                (m.reasons || []).forEach(function (r) { card.appendChild(el('div', 'opp-reason', r)); });
                list.appendChild(card);
            });
        }).catch(function () { /* section stays hidden */ });
    })();

    /* ---- deadline alerts (authed only, bookmarked only; silent on failure) ---- */
    (function alerts() {
        var sec = document.getElementById('oppAlerts');
        var buckets = document.getElementById('oppAlertBuckets');
        if (!sec || !buckets) return;
        var order = [
            { key: 'due_1d', label: 'Due tomorrow' },
            { key: 'due_3d', label: 'Due in 3 days' },
            { key: 'due_7d', label: 'Due this week' },
        ];
        getJSON('/api/opportunities/alerts').then(function (res) {
            var b = res.body;
            if (!b || !b.success || !b.alerts) return;
            var any = false;
            order.forEach(function (o) {
                var items = b.alerts[o.key];
                if (!Array.isArray(items) || !items.length) return;
                any = true;
                var wrap = el('div');
                wrap.appendChild(el('span', 'due-label', o.label));
                var ul = el('ul');
                items.forEach(function (it) {
                    var li = el('li');
                    var a = el('a', null, it.title || it.opp_id);
                    a.href = it.url || '#';
                    a.target = '_blank';
                    a.rel = 'noopener';
                    li.appendChild(a);
                    var dl = typeof it.days_left === 'number'
                        ? (it.days_left === 0 ? ' · due today' : it.days_left === 1 ? ' · tomorrow' : ' · in ' + it.days_left + ' days')
                        : (it.deadline ? ' · ' + it.deadline : '');
                    li.appendChild(document.createTextNode(dl));
                    ul.appendChild(li);
                });
                wrap.appendChild(ul);
                buckets.appendChild(wrap);
            });
            if (any) sec.hidden = false;
        }).catch(function () { /* banner stays hidden */ });
    })();

    if (!grid) return;

    grid.addEventListener('click', function (ev) {
        var btn = ev.target.closest('.opp-bm');
        if (!btn) return;
        var card = btn.closest('[data-opp-id]');
        if (!card) return;
        var oppId = card.getAttribute('data-opp-id');
        var saved = btn.getAttribute('data-bookmarked') === '1';
        btn.disabled = true;
        fetch(saved ? '/opportunities/bookmark/remove' : '/opportunities/bookmark', {
            method: 'POST',
            headers: headers(),
            body: JSON.stringify({ opp_id: oppId }),
        })
            .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
            .then(function (res) {
                btn.disabled = false;
                if (!res.body || !res.body.success) return;
                var nowSaved = !saved;
                btn.setAttribute('data-bookmarked', nowSaved ? '1' : '0');
                btn.classList.toggle('saved', nowSaved);
                btn.textContent = nowSaved ? '★ Saved' : '☆ Save';
            })
            .catch(function () { btn.disabled = false; });
    });

    /* ---- Phase 3 per-card actions: tracker, .ics, eligibility ---- */
    grid.addEventListener('click', function (ev) {
        var card = ev.target.closest('[data-opp-id]');
        if (!card) return;
        var oppId = card.getAttribute('data-opp-id');

        var trackBtn = ev.target.closest('.opp-track');
        if (trackBtn) {
            if (trackBtn.getAttribute('data-track-state') !== 'idle') return;
            trackBtn.setAttribute('data-track-state', 'busy');
            trackBtn.disabled = true;
            fetch('/api/opportunities/' + encodeURIComponent(oppId) + '/apply-to-tracker', {
                method: 'POST', headers: headers(), body: JSON.stringify({}),
            })
                .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
                .then(function (res) {
                    trackBtn.disabled = false;
                    if (res.status === 201) {
                        trackBtn.textContent = 'In tracker ✓';
                        trackBtn.classList.add('done');
                        trackBtn.setAttribute('data-track-state', 'done');
                        var view = el('a', 'opp-link', 'View tracker →');
                        view.href = '/tracker';
                        trackBtn.parentNode.appendChild(view);
                    } else if (res.status === 200) {
                        trackBtn.textContent = 'Already in tracker';
                        trackBtn.classList.add('done');
                        trackBtn.setAttribute('data-track-state', 'done');
                    } else {
                        trackBtn.setAttribute('data-track-state', 'idle');
                        showInlineMsg(card, (res.body && res.body.error) || 'Could not add to tracker.', true);
                    }
                })
                .catch(function () {
                    trackBtn.disabled = false;
                    trackBtn.setAttribute('data-track-state', 'idle');
                    showInlineMsg(card, 'Network error. Please try again.', true);
                });
            return;
        }

        var icsBtn = ev.target.closest('.opp-ics');
        if (icsBtn) {
            icsBtn.disabled = true;
            fetch('/api/opportunities/' + encodeURIComponent(oppId) + '/ics', { headers: headers() })
                .then(function (r) {
                    if (!r.ok) throw new Error('no-deadline');
                    return r.blob();
                })
                .then(function (blob) {
                    icsBtn.disabled = false;
                    var url = URL.createObjectURL(blob);
                    var a = document.createElement('a');
                    a.href = url;
                    a.download = 'opportunity-' + oppId + '.ics';
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
                })
                .catch(function () {
                    icsBtn.disabled = false;
                    showInlineMsg(card, 'No deadline set for this opportunity', true);
                });
            return;
        }

        var eligBtn = ev.target.closest('.opp-elig-btn');
        if (eligBtn) {
            var detail = document.getElementById('elig-' + oppId);
            if (!detail) return;
            var expanded = eligBtn.getAttribute('aria-expanded') === 'true';
            if (expanded) {
                detail.hidden = true;
                eligBtn.setAttribute('aria-expanded', 'false');
                return;
            }
            eligBtn.setAttribute('aria-expanded', 'true');
            if (detail.getAttribute('data-loaded') === '1') { detail.hidden = false; return; }
            detail.textContent = 'Loading…';
            detail.hidden = false;
            getJSON('/api/opportunities/' + encodeURIComponent(oppId)).then(function (res) {
                var b = res.body;
                // Prefer the split checklist; fall back to the raw string.
                var items = b && b.success && b.opportunity &&
                    (b.opportunity.eligibility_list || b.opportunity.eligibility);
                detail.textContent = '';
                if (Array.isArray(items) && items.length) {
                    var ul = el('ul');
                    items.forEach(function (it) { ul.appendChild(el('li', null, it)); });
                    detail.appendChild(ul);
                } else if (typeof items === 'string' && items) {
                    detail.appendChild(el('div', null, items));
                } else {
                    detail.appendChild(el('div', null, 'Eligibility not listed for this opportunity'));
                }
                detail.setAttribute('data-loaded', '1');
            }).catch(function () {
                detail.textContent = 'Could not load eligibility. Please try again.';
            });
        }
    });
})();
