/**
 * Career Discovery Assessment (Wave 1, feature #7) - Frontend
 * Stepper quiz UI: one question at a time, answers collected client-side,
 * submitted as JSON with the CSRF header. All content HTML-escaped.
 */

(function () {
    'use strict';

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    var DIM_LABELS = {
        interests: 'Your interests',
        strengths: 'Your strengths',
        workstyle: 'Work style',
        subjects: 'Subjects you enjoy'
    };

    var questions = window.__ASSESSMENT_QUESTIONS__ || [];
    var answers = {};   // question_id -> option_index
    var current = 0;

    function $(id) { return document.getElementById(id); }

    // Phase 2 handoff helpers: the backend may expose role cards and
    // handoff links under several field names; resolve tolerantly and
    // never render a dead link or an empty tracker button.
    function pick(obj) {
        for (var i = 1; i < arguments.length; i++) {
            var v = obj ? obj[arguments[i]] : undefined;
            if (v !== undefined && v !== null && v !== '') return v;
        }
        return undefined;
    }

    function safeUrl(u) {
        if (typeof u !== 'string' || !u) return null;
        if (u.charAt(0) === '/' || /^https?:\/\//i.test(u)) return u;
        return null;
    }

    function linkFrom(raw) {
        if (typeof raw === 'string') return { url: safeUrl(raw), title: null };
        if (raw && typeof raw === 'object') {
            return { url: safeUrl(raw.url || raw.link || null), title: raw.title || null };
        }
        return { url: null, title: null };
    }

    function csrfHeaders(extra) {
        var headers = extra || {};
        var tokenMeta = document.querySelector('meta[name="csrf-token"]');
        if (tokenMeta) headers['X-CSRFToken'] = tokenMeta.getAttribute('content');
        return headers;
    }

    function render() {
        var q = questions[current];
        if (!q) return;
        $('asDim').textContent = (DIM_LABELS[q.dimension] || q.dimension)
            + '  ·  Question ' + (current + 1) + ' of ' + questions.length;
        $('asQ').textContent = q.text;
        var box = $('asOpts');
        box.innerHTML = '';
        q.options.forEach(function (optText, idx) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'as-opt' + (answers[q.id] === idx ? ' selected' : '');
            btn.textContent = optText;
            btn.addEventListener('click', function () {
                answers[q.id] = idx;
                Array.prototype.forEach.call(box.children, function (c) {
                    c.classList.remove('selected');
                });
                btn.classList.add('selected');
                $('asError').hidden = true;
                // Auto-advance after a short beat (skip on reduced motion).
                var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
                setTimeout(next, reduce ? 0 : 220);
            });
            box.appendChild(btn);
        });
        $('asBar').style.width = Math.round(100 * current / questions.length) + '%';
        $('asBack').style.visibility = current === 0 ? 'hidden' : 'visible';
        $('asNext').textContent = current === questions.length - 1 ? 'See my results' : 'Next';
    }

    function next() {
        var q = questions[current];
        if (answers[q.id] === undefined) {
            var err = $('asError');
            err.textContent = 'Please pick an option to continue.';
            err.hidden = false;
            return;
        }
        if (current < questions.length - 1) {
            current += 1;
            render();
        } else {
            submit();
        }
    }

    function back() {
        if (current > 0) { current -= 1; render(); }
    }

    function submit() {
        var btn = $('asNext');
        btn.disabled = true;
        btn.textContent = 'Analyzing…';
        var headers = { 'Content-Type': 'application/json' };
        var tokenMeta = document.querySelector('meta[name="csrf-token"]');
        if (tokenMeta) headers['X-CSRFToken'] = tokenMeta.getAttribute('content');
        fetch('/assessment/submit', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({ answers: answers })
        })
            .then(function (resp) {
                return resp.json().then(function (data) {
                    return { status: resp.status, data: data };
                });
            })
            .then(function (out) {
                if (out.status === 429) {
                    showError('Too many attempts. Please try again later.');
                    return;
                }
                if (!out.data || !out.data.success) {
                    showError((out.data && out.data.error) || 'Could not score your assessment.');
                    return;
                }
                renderResults(out.data.result, out.data.saved_url);
            })
            .catch(function () { showError('Something went wrong. Check your connection and retry.'); })
            .finally(function () { btn.disabled = false; btn.textContent = 'See my results'; });
    }

    function showError(msg) {
        var err = $('asError');
        err.textContent = msg;
        err.hidden = false;
        err.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    function renderResults(result, savedUrl, mount) {
        var quiz = $('asQuiz');
        if (quiz) quiz.hidden = true;
        var box = mount || $('asResult');
        if (!box) return;
        box.hidden = false;
        var html = '<div class="as-head"><h1>Your <span class="gradient-text">matches</span></h1>';
        html += '<p>Based on your answers, here are your top career paths. '
            + '<a href="' + esc(savedUrl || '/dashboard') + '" style="color:var(--accent-secondary);">View saved result</a> · '
            + '<a href="/assessment" style="color:var(--accent-secondary);">Retake quiz</a></p></div>';
        if (result.ai_powered) {
            html += '<div class="as-ai-badge">AI-personalized explanations</div>';
        }
        (result.clusters || []).forEach(function (c, i) {
            html += '<div class="as-result-card">';
            html += '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:1rem;">'
                + '<h3>#' + (i + 1) + ' ' + esc(c.name) + '</h3>'
                + '<span class="as-match">' + esc(c.match) + '%</span></div>';
            html += '<div class="as-tagline">' + esc(c.tagline) + '</div>';
            if (c.why) html += '<div class="as-why">' + esc(c.why) + '</div>';
            html += '<div style="font-size:0.85rem;font-weight:700;margin-bottom:0.4rem;">Skills to build</div>';
            html += '<div class="as-skills">' + (c.skills || []).map(function (s) {
                return '<span class="as-chip">' + esc(s) + '</span>';
            }).join('') + '</div>';
            html += '<div class="as-tools"><span style="font-size:0.85rem;font-weight:700;">Try next: </span>';
            (c.tools || []).forEach(function (t, ti) {
                var link = (c.tool_links || [])[ti] || '/resume';
                html += '<a href="' + esc(link) + '">' + esc(t) + '</a>';
            });
            html += '</div>';
            // Phase 2: role cards + handoffs (render only what the backend sent).
            var roleCards = pick(c, 'role_cards', 'roles', 'career_roles', 'cards');
            if (Array.isArray(roleCards) && roleCards.length) {
                html += '<div style="font-size:0.85rem;font-weight:700;margin:1rem 0 0.4rem;">Roles to explore</div>';
                html += '<div class="as-role-grid">';
                roleCards.forEach(function (r) {
                    var title = (r && (r.title || r.name)) || '';
                    var desc = (r && (r.description || r.blurb || r.summary)) || '';
                    var url = safeUrl(r && (r.url || r.link));
                    html += '<div class="b-card b-card--lite as-role-card"><h4>' + esc(title) + '</h4>';
                    if (desc) html += '<p>' + esc(desc) + '</p>';
                    if (Array.isArray(r.skills) && r.skills.length) {
                        html += '<div class="as-skills">' + r.skills.map(function (s) {
                            return '<span class="as-chip">' + esc(s) + '</span>';
                        }).join('') + '</div>';
                    }
                    if (url) html += '<a class="as-role-link" href="' + esc(url) + '">Explore role &rarr;</a>';
                    html += '</div>';
                });
                html += '</div>';
            }
            var trackerPayload = pick(c, 'tracker_payload');
            var primaryRole = pick(c, 'tracker_role', 'primary_role', 'tracker_role_title');
            if (!primaryRole && Array.isArray(roleCards) && roleCards.length) {
                primaryRole = roleCards[0].title || roleCards[0].name || null;
            }
            var roadmap = linkFrom(pick(c, 'roadmap_url', 'roadmap_link', 'roadmap'));
            if (!roadmap.url) {
                // Backend handoffs may carry a roadmap skill id instead of a URL.
                var roadmapSkill = pick(c, 'roadmap_skill');
                if (roadmapSkill) roadmap = { url: '/learn/roadmaps/' + encodeURIComponent(roadmapSkill), title: null };
            }
            var quiz = linkFrom(pick(c, 'quiz_url', 'quiz_link', 'quiz'));
            if (!quiz.url && pick(c, 'quiz_skill')) {
                // No quiz deep-link param exists; the picker page is the honest target.
                quiz = { url: '/learn/quizzes', title: null };
            }
            var showTrack = !!(trackerPayload || primaryRole);
            if (showTrack || roadmap.url || quiz.url) {
                var tp = (trackerPayload && typeof trackerPayload === 'object') ? trackerPayload : null;
                var tCompany = tp ? (tp.company || '') : (pick(c, 'tracker_company') || c.name || '');
                var tRole = tp ? (tp.role || '') : (primaryRole || '');
                html += '<div class="as-handoffs">';
                if (showTrack) {
                    html += '<button type="button" class="as-track-btn" data-company="' + esc(tCompany) + '" data-role="' + esc(tRole) + '" data-cluster="' + esc(c.name || '') + '">Add to tracker</button>';
                    html += '<span class="as-track-msg" aria-live="polite"></span>';
                }
                if (roadmap.url) html += '<a class="as-handoff-link" href="' + esc(roadmap.url) + '">' + esc(roadmap.title || 'View roadmap') + '</a>';
                if (quiz.url) html += '<a class="as-handoff-link" href="' + esc(quiz.url) + '">' + esc(quiz.title || 'Practice quiz') + '</a>';
                html += '</div>';
            }
            html += '</div>';
        });
        box.innerHTML = html;
        // Wire "Add to tracker" buttons (delegated per rendered button).
        Array.prototype.forEach.call(box.querySelectorAll('.as-track-btn'), function (btn) {
            btn.addEventListener('click', function () {
                var msg = btn.nextElementSibling;
                var company = btn.getAttribute('data-company') || '';
                var role = btn.getAttribute('data-role') || '';
                var cluster = btn.getAttribute('data-cluster') || '';
                if (!company || !role) {
                    if (msg) msg.textContent = 'This path has no role to track yet.';
                    return;
                }
                btn.disabled = true;
                btn.textContent = 'Adding…';
                fetch('/tracker/add', {
                    method: 'POST',
                    headers: csrfHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify({
                        company: company, role: role, stage: 'wishlist',
                        notes: cluster ? ('From career discovery: ' + cluster) : 'From career discovery'
                    })
                })
                    .then(function (resp) {
                        return resp.json().then(function (data) {
                            return { status: resp.status, data: data };
                        });
                    })
                    .then(function (out) {
                        if (out.status === 201 || (out.data && out.data.success)) {
                            btn.textContent = 'Added to tracker';
                            if (msg) msg.textContent = '';
                            var link = document.createElement('a');
                            link.href = '/tracker';
                            link.className = 'as-handoff-link';
                            link.textContent = 'Open tracker';
                            if (msg) msg.appendChild(link);
                        } else if (out.status === 401) {
                            btn.disabled = false;
                            btn.textContent = 'Add to tracker';
                            if (msg) msg.textContent = 'Log in to track this path.';
                        } else {
                            btn.disabled = false;
                            btn.textContent = 'Add to tracker';
                            if (msg) msg.textContent = (out.data && out.data.error) || 'Could not add to tracker.';
                        }
                    })
                    .catch(function () {
                        btn.disabled = false;
                        btn.textContent = 'Add to tracker';
                        if (msg) msg.textContent = 'Could not add to tracker. Check your connection.';
                    });
            });
        });
        window.scrollTo({ top: 0, behavior: 'smooth' });
        window.renderAssessmentResults = renderResults;
    }

    // Phase 2: past attempts on the saved-result page. Silent on failure.
    function initHistory() {
        var sec = document.getElementById('asHistory');
        if (!sec) return;
        fetch('/api/career/assessment/history', { headers: { 'Accept': 'application/json' } })
            .then(function (resp) {
                if (!resp.ok) throw new Error('history unavailable');
                return resp.json();
            })
            .then(function (body) {
                if (!body || !body.success) throw new Error('history unavailable');
                var list = document.getElementById('asHistoryList');
                var countEl = document.getElementById('asHistoryCount');
                if (!list) return;
                var attempts = body.attempts || [];
                var count = (body.count != null ? body.count : attempts.length);
                if (countEl) countEl.textContent = count + ' past attempt' + (count === 1 ? '' : 's');
                list.textContent = '';
                if (!attempts.length) {
                    list.textContent = 'No past attempts yet — your first result is above.';
                } else {
                    attempts.forEach(function (a) {
                        var row = document.createElement('div');
                        row.className = 'as-history-row';
                        var d = a.completed_at ? new Date(a.completed_at) : null;
                        var dateStr = (d && !isNaN(d.getTime()))
                            ? d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
                            : (a.completed_at || '');
                        var namesRaw = a.top_cluster_names || a.top_clusters || [];
                        var names = namesRaw.map(function (n) {
                            return (n && typeof n === 'object') ? (n.name || n.id || '') : String(n);
                        }).filter(function (n) { return !!n; });
                        row.appendChild(document.createTextNode(
                            (dateStr ? dateStr + ' — ' : '') + (names.length ? names.join(', ') : 'Assessment')));
                        list.appendChild(row);
                    });
                }
                sec.hidden = false;
            })
            .catch(function () { sec.hidden = true; });
    }

    // Saved-result page: server embeds clusters JSON; render on load.
    function initSavedResult() {
        var mount = document.getElementById('asSavedResult');
        if (!mount || !window.__ASSESSMENT_RESULT__) return;
        var data = window.__ASSESSMENT_RESULT__;
        renderResults({ clusters: data.clusters || [], ai_powered: !!data.ai_powered }, null, mount);
        var head = mount.querySelector('.as-head p');
        if (head) head.innerHTML = '<a href="/dashboard" style="color:var(--accent-secondary);">&larr; Back to dashboard</a> · <a href="/assessment" style="color:var(--accent-secondary);">Retake quiz</a>';
        initHistory();
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (document.getElementById('asQuiz')) {
            if (!questions.length) {
                showError('Could not load the quiz. Please reload the page.');
                return;
            }
            render();
            $('asNext').addEventListener('click', next);
            $('asBack').addEventListener('click', back);
        }
        initSavedResult();
    });
})();
