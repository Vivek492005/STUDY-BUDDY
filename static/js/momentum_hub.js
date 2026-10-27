/* Momentum hub: quest claims, league join/leave, reflection save, opt-out.
   Wave 5 #27. All POSTs carry the CSRF token via the X-CSRFToken header. */
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
    function hideErr(id) {
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

    /* ---- quest claims ---- */
    var claimBtns = document.querySelectorAll('.mm-claim');
    Array.prototype.forEach.call(claimBtns, function (btn) {
        btn.addEventListener('click', function () {
            hideErr('mm-quest-error');
            btn.disabled = true;
            post('/momentum/api/quest/' + encodeURIComponent(btn.getAttribute('data-quest')) + '/claim')
                .then(function (data) {
                    if (data.error) {
                        btn.disabled = false;
                        showErr('mm-quest-error', data.error);
                        return;
                    }
                    window.location.reload();
                })
                .catch(function () {
                    btn.disabled = false;
                    showErr('mm-quest-error', 'Network error — please try again.');
                });
        });
    });

    /* ---- league join / leave ---- */
    var joinBtn = $('mm-league-join');
    if (joinBtn) {
        joinBtn.addEventListener('click', function () {
            hideErr('mm-league-error');
            joinBtn.disabled = true;
            var st = $('mm-league-status');
            if (st) { st.textContent = 'Joining…'; st.style.display = 'inline-flex'; }
            post('/momentum/api/league/join')
                .then(function (data) {
                    if (data.error) {
                        joinBtn.disabled = false;
                        if (st) st.style.display = 'none';
                        showErr('mm-league-error', data.error);
                        return;
                    }
                    window.location.reload();
                })
                .catch(function () {
                    joinBtn.disabled = false;
                    if (st) st.style.display = 'none';
                    showErr('mm-league-error', 'Network error — please try again.');
                });
        });
    }
    var leaveBtn = $('mm-league-leave');
    if (leaveBtn) {
        leaveBtn.addEventListener('click', function () {
            hideErr('mm-league-error');
            leaveBtn.disabled = true;
            post('/momentum/api/league/leave')
                .then(function (data) {
                    if (data.error) {
                        leaveBtn.disabled = false;
                        showErr('mm-league-error', data.error);
                        return;
                    }
                    window.location.reload();
                })
                .catch(function () {
                    leaveBtn.disabled = false;
                    showErr('mm-league-error', 'Network error — please try again.');
                });
        });
    }

    /* ---- reflection ---- */
    var saveBtn = $('mm-reflect-save');
    if (saveBtn) {
        saveBtn.addEventListener('click', function () {
            hideErr('mm-reflect-error');
            var st = $('mm-reflect-status');
            saveBtn.disabled = true;
            if (st) { st.textContent = 'Saving…'; st.style.display = 'inline-flex'; }
            post('/momentum/api/reflect', {
                wins: ($('mm-wins') || {}).value || '',
                focus_next: ($('mm-focus') || {}).value || ''
            })
                .then(function (data) {
                    saveBtn.disabled = false;
                    if (st) st.style.display = 'none';
                    if (data.error) { showErr('mm-reflect-error', data.error); return; }
                    if (st) { st.textContent = 'Saved — nice reflection.'; st.style.display = 'inline-flex'; }
                })
                .catch(function () {
                    saveBtn.disabled = false;
                    if (st) st.style.display = 'none';
                    showErr('mm-reflect-error', 'Network error — please try again.');
                });
        });
    }

    /* ---- opt-out / rejoin ---- */
    function setOptOut(optOut, btnId, errId, statusId) {
        var btn = $(btnId);
        if (!btn) return;
        btn.addEventListener('click', function () {
            hideErr(errId);
            btn.disabled = true;
            var st = $(statusId);
            if (st) { st.textContent = 'Updating…'; st.style.display = 'inline-flex'; }
            post('/momentum/api/opt-out', { opt_out: !!optOut })
                .then(function (data) {
                    if (data.error) {
                        btn.disabled = false;
                        if (st) st.style.display = 'none';
                        showErr(errId, data.error);
                        return;
                    }
                    window.location.reload();
                })
                .catch(function () {
                    btn.disabled = false;
                    if (st) st.style.display = 'none';
                    showErr(errId, 'Network error — please try again.');
                });
        });
    }
    setOptOut(true, 'mm-optout', 'mm-optout-error', null);
    setOptOut(false, 'mm-rejoin', 'mm-error', 'mm-status');
})();

/* Phase 4: badges shelf, team leagues, weekly recap, streak repair.
   All data comes from the backend APIs — nothing is computed here.
   Failures hide the bonus sections silently; they are never errors. */
(function () {
    'use strict';
    function csrfHeaders(extra) {
        var headers = extra || {};
        var m = document.querySelector('meta[name="csrf-token"]');
        if (m) headers['X-CSRFToken'] = m.getAttribute('content');
        return headers;
    }
    function $(id) { return document.getElementById(id); }
    function el(tag, text, cls) {
        var node = document.createElement(tag);
        if (text !== null && text !== undefined) node.textContent = text;
        if (cls) node.className = cls;
        return node;
    }
    function get(url) {
        return fetch(url, { headers: csrfHeaders({}) })
            .then(function (r) { return r.json().catch(function () { return {}; }); });
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

    /* ---- badges shelf ---- */
    var badgesSec = $('mm-badges');
    var badgesList = $('mm-badges-list');
    if (badgesSec && badgesList) {
        get('/api/momentum/badges').then(function (data) {
            var badges = (data && data.badges) || [];
            if (!data || data.error || !badges.length) {
                badgesSec.style.display = 'none';
                return;
            }
            badges.forEach(function (b) {
                var card = el('article', null, 'b-card b-card--feature b-reveal');
                var tag = el('span', b.earned ? 'Earned' : 'Not yet earned', 'b-tag');
                card.appendChild(tag);
                card.appendChild(el('h3', b.title || '', null)).style.margin = '.5rem 0';
                card.appendChild(el('p', b.description || '', null)).style.cssText =
                    'color:var(--b-muted);font-size:.88rem;margin:0;';
                if (b.earned && b.earned_at) {
                    var when = el('p', 'Earned ' + b.earned_at, null);
                    when.style.cssText = 'font-size:.85rem;margin:.5rem 0 0;';
                    card.appendChild(when);
                } else {
                    card.style.opacity = '.65';
                }
                badgesList.appendChild(card);
            });
        }).catch(function () {
            badgesSec.style.display = 'none';
        });
    }

    /* ---- team leagues ---- */
    var teamList = $('mm-team-leagues-list');
    function renderTeamLeagues(leagues) {
        if (!teamList) return;
        while (teamList.firstChild) teamList.removeChild(teamList.firstChild);
        if (!leagues || !leagues.length) {
            teamList.appendChild(el('p', 'You are not in any team league yet — create one or join with a code below.', null))
                .style.color = 'var(--b-muted)';
            return;
        }
        leagues.forEach(function (lg) {
            var row = el('p', null, null);
            row.style.margin = '.4rem 0';
            row.appendChild(el('strong', lg.name || 'League', null));
            row.appendChild(document.createTextNode(
                ' · ' + (lg.members_count != null ? lg.members_count : '?') + ' members' +
                (lg.my_rank != null ? ' · your rank #' + lg.my_rank : '') +
                (lg.my_xp != null ? ' · your XP ' + lg.my_xp : '')));
            teamList.appendChild(row);
        });
    }
    function refreshTeamLeagues() {
        get('/api/momentum/leagues').then(function (data) {
            if (data && !data.error) renderTeamLeagues(data.leagues);
        }).catch(function () { /* leave the list as-is */ });
    }
    if (teamList) refreshTeamLeagues();
    var createForm = $('mm-team-create');
    if (createForm) {
        createForm.addEventListener('submit', function (ev) {
            ev.preventDefault();
            var err = $('mm-team-error');
            if (err) err.style.display = 'none';
            var name = ($('mm-team-name') || {}).value || '';
            if (!name.trim()) {
                if (err) { err.textContent = 'Give your league a name first.'; err.style.display = 'block'; }
                return;
            }
            post('/api/momentum/leagues', { name: name.trim() }).then(function (data) {
                if (!data || data.error || !data.league) {
                    if (err) { err.textContent = (data && data.error) || 'Could not create the league.'; err.style.display = 'block'; }
                    return;
                }
                var created = $('mm-team-created');
                if (created) {
                    created.textContent = '';
                    created.appendChild(el('strong', 'League created: ' + (data.league.name || name), null));
                    if (data.league.invite_code) {
                        created.appendChild(document.createTextNode(' — invite code: '));
                        created.appendChild(el('strong', data.league.invite_code, null));
                        created.appendChild(document.createTextNode(' (share this code with your team)'));
                    }
                    created.style.display = 'block';
                }
                refreshTeamLeagues();
            }).catch(function () {
                if (err) { err.textContent = 'Network error — please try again.'; err.style.display = 'block'; }
            });
        });
    }
    var joinForm = $('mm-team-join');
    if (joinForm) {
        joinForm.addEventListener('submit', function (ev) {
            ev.preventDefault();
            var err = $('mm-team-error');
            if (err) err.style.display = 'none';
            var code = ($('mm-team-code') || {}).value || '';
            if (!code.trim()) {
                if (err) { err.textContent = 'Paste an invite code first.'; err.style.display = 'block'; }
                return;
            }
            post('/api/momentum/leagues/join', { invite_code: code.trim() }).then(function (data) {
                if (!data || data.error) {
                    if (err) { err.textContent = (data && data.error) || 'Could not join that league — check the code.'; err.style.display = 'block'; }
                    return;
                }
                refreshTeamLeagues();
            }).catch(function () {
                if (err) { err.textContent = 'Network error — please try again.'; err.style.display = 'block'; }
            });
        });
    }

    /* ---- weekly recap ---- */
    var recapSec = $('mm-recap');
    var recapBody = $('mm-recap-body');
    if (recapSec && recapBody) {
        get('/api/momentum/recap').then(function (data) {
            var recap = data && data.recap;
            if (!data || data.error || !recap) {
                recapSec.style.display = 'none';
                return;
            }
            var lines = [];
            if (recap.week_start) lines.push('Week of ' + recap.week_start);
            if (recap.xp_earned != null) lines.push(recap.xp_earned + ' XP earned');
            if (recap.streak_days != null) lines.push(recap.streak_days + '-day streak');
            if (recap.applications_added != null) lines.push(recap.applications_added + ' applications added');
            if (recap.applications_moved != null) lines.push(recap.applications_moved + ' applications moved');
            if (recap.referrals_asked != null) lines.push(recap.referrals_asked + ' referrals asked');
            var head = el('p', lines.join(' · '), null);
            head.style.cssText = 'color:var(--b-muted);font-size:.9rem;margin:0 0 .8rem;';
            recapBody.appendChild(head);
            if (recap.quests_completed && recap.quests_completed.length) {
                recapBody.appendChild(el('h3', 'Quests completed', null)).style.margin = '.5rem 0';
                var ul = el('ul', null, null);
                recap.quests_completed.forEach(function (t) {
                    ul.appendChild(el('li', t, null));
                });
                recapBody.appendChild(ul);
            }
            if (recap.top_action) {
                var top = el('p', null, null);
                top.appendChild(el('strong', 'Top action: ', null));
                top.appendChild(document.createTextNode(recap.top_action));
                recapBody.appendChild(top);
            }
        }).catch(function () {
            recapSec.style.display = 'none';
        });
    }

    /* ---- streak repair ---- */
    var repairBtn = $('mm-streak-repair');
    if (repairBtn) {
        repairBtn.addEventListener('click', function () {
            if (!window.confirm('Repair your streak? You can do this once per 30 days.')) return;
            var err = $('mm-streak-repair-error');
            var st = $('mm-streak-repair-status');
            if (err) err.style.display = 'none';
            repairBtn.disabled = true;
            post('/api/momentum/streak/repair', {}).then(function (data) {
                repairBtn.disabled = false;
                if (!data || data.error || data.success === false) {
                    var reason = (data && (data.error || data.reason)) || 'Could not repair your streak.';
                    if (err) { err.textContent = reason; err.style.display = 'block'; }
                    return;
                }
                var cur = data.streak && (data.streak.current != null ? data.streak.current : data.streak);
                if (typeof cur === 'number') {
                    var ring = $('mm-streak-ring-num');
                    var heading = $('mm-streak-heading');
                    if (ring) ring.textContent = cur;
                    if (heading) heading.textContent = cur + '-day streak';
                    if (st) { st.textContent = 'Streak repaired.'; st.style.display = 'inline-flex'; }
                } else {
                    window.location.reload();
                }
            }).catch(function () {
                repairBtn.disabled = false;
                if (err) { err.textContent = 'Network error — please try again.'; err.style.display = 'block'; }
            });
        });
    }
})();
