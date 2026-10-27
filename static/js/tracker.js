/**
 * Application Tracker Kanban (Wave 1, feature #6) - Frontend
 * Drag-and-drop between stages (HTML5) with a move-dropdown fallback for
 * touch devices. All mutations go through CSRF-protected JSON endpoints.
 * All user content is HTML-escaped before insertion.
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

    function csrfHeaders() {
        var headers = { 'Content-Type': 'application/json' };
        var tokenMeta = document.querySelector('meta[name="csrf-token"]');
        if (tokenMeta) headers['X-CSRFToken'] = tokenMeta.getAttribute('content');
        return headers;
    }

    function setError(msg) {
        var box = document.getElementById('tkError');
        if (!box) return;
        box.textContent = msg || '';
        box.hidden = !msg;
    }

    function api(path, payload) {
        return fetch(path, {
            method: 'POST',
            headers: csrfHeaders(),
            body: JSON.stringify(payload || {})
        }).then(function (resp) {
            return resp.json().then(function (data) {
                return { status: resp.status, data: data };
            });
        });
    }

    function stageLabel(stage) {
        var sel = document.querySelector('.tk-move option[value="' + stage + '"]');
        return sel ? sel.textContent : stage;
    }

    function cardHtml(a) {
        var html = '<div class="tk-card b-kcard" draggable="true" data-id="' + a.id + '" data-stage="' + esc(a.stage) + '">';
        html += '<h4>' + esc(a.company) + '</h4>';
        html += '<div class="tk-role">' + esc(a.role) + '</div>';
        if (a.applied_date) html += '<div class="tk-meta">Applied ' + esc(a.applied_date) + '</div>';
        if (a.notes) html += '<div class="tk-notes">' + esc(a.notes) + '</div>';
        html += '<div class="tk-dl-line"' + (a.deadline ? '' : ' hidden') + '>' + (a.deadline ? '📅 Deadline ' + esc(a.deadline) : '') + '</div>';
        html += '<div class="tk-dl-row">';
        html += '<input type="date" class="tk-dl-date" aria-label="Deadline date" value="' + esc(a.deadline || '') + '">';
        html += '<select class="tk-dl-kind" aria-label="Deadline kind">' +
            ['oa', 'interview', 'followup', 'offer', 'other'].map(function (k) {
                return '<option value="' + k + '"' + (a.deadline_kind === k ? ' selected' : '') + '>' + k + '</option>';
            }).join('') + '</select>';
        html += '<button class="tk-mini tk-dl-save" type="button">Save deadline</button>';
        html += '</div><div class="tk-dl-error tk-error" hidden></div>';
        html += '<div class="tk-actions">';
        if (a.jd_url) html += '<a class="tk-mini" href="' + esc(a.jd_url) + '" target="_blank" rel="noopener" style="text-decoration:none;">JD link</a>';
        html += '<select class="tk-move" aria-label="Move to stage">' + moveOptions(a.stage) + '</select>';
        html += '<button class="tk-mini tk-edit" type="button">Edit</button>';
        html += '<button class="tk-mini danger tk-del" type="button">Delete</button>';
        html += '<button class="tk-mini tk-kit-btn" type="button">Interview kit</button>';
        html += '<button class="tk-mini tk-timeline-btn" type="button" aria-expanded="false">Timeline</button>';
        html += '</div><div class="tk-timeline" hidden></div></div>';
        return html;
    }

    function moveOptions(current) {
        var stages = [];
        document.querySelectorAll('.tk-col').forEach(function (col) {
            stages.push({ id: col.getAttribute('data-stage'), label: col.querySelector('h3').textContent });
        });
        return stages.map(function (s) {
            return '<option value="' + esc(s.id) + '"' + (s.id === current ? ' selected' : '') + '>' + esc(s.label) + '</option>';
        }).join('');
    }

    function refreshColumnCounts() {
        document.querySelectorAll('.tk-col').forEach(function (col) {
            var n = col.querySelectorAll('.tk-card').length;
            var badge = col.querySelector('.tk-count');
            if (badge) badge.textContent = n;
        });
    }

    function moveCardInDom(card, newStage) {
        var target = document.querySelector('.tk-col[data-stage="' + newStage + '"] .tk-col-body');
        if (!target) return;
        card.setAttribute('data-stage', newStage);
        var sel = card.querySelector('.tk-move');
        if (sel) sel.value = newStage;
        target.appendChild(card);
        refreshColumnCounts();
        refreshStats();
    }

    function refreshStats() {
        fetch('/tracker/stats').then(function (r) { return r.json(); }).then(function (data) {
            var stats = (data && data.stats) || {};
            var counts = stats.counts || {};
            var box = document.getElementById('tkStats');
            if (!box) return;
            var html = '<div class="tk-stat"><strong>' + esc(stats.total || 0) + '</strong><span>Total tracked</span></div>';
            html += '<div class="tk-stat"><strong>' + esc(stats.response_rate || 0) + '%</strong><span>Response rate</span></div>';
            document.querySelectorAll('.tk-col').forEach(function (col) {
                var sid = col.getAttribute('data-stage');
                var label = col.querySelector('h3').textContent;
                html += '<div class="tk-stat"><strong>' + esc(counts[sid] || 0) + '</strong><span>' + esc(label) + '</span></div>';
            });
            box.innerHTML = html;
        }).catch(function () { /* stats are best-effort */ });
    }

    function bindCard(card) {
        card.addEventListener('dragstart', function (e) {
            e.dataTransfer.setData('text/plain', card.getAttribute('data-id'));
            e.dataTransfer.effectAllowed = 'move';
            card.classList.add('tk-dragging');
        });
        card.addEventListener('dragend', function () {
            card.classList.remove('tk-dragging');
            document.querySelectorAll('.tk-col').forEach(function (c) { c.classList.remove('tk-drop'); });
        });

        var moveSel = card.querySelector('.tk-move');
        if (moveSel) {
            moveSel.addEventListener('change', function () {
                doMove(card.getAttribute('data-id'), moveSel.value, card);
            });
        }
        var editBtn = card.querySelector('.tk-edit');
        if (editBtn) editBtn.addEventListener('click', function () { openModal(card); });
        var delBtn = card.querySelector('.tk-del');
        if (delBtn) {
            delBtn.addEventListener('click', function () {
                if (!window.confirm('Delete this application?')) return;
                var id = card.getAttribute('data-id');
                api('/tracker/' + id + '/delete').then(function (out) {
                    if (!out.data || !out.data.success) {
                        setError((out.data && out.data.error) || 'Delete failed.');
                        return;
                    }
                    card.remove();
                    refreshColumnCounts();
                    refreshStats();
                }).catch(function () { setError('Delete failed. Check your connection.'); });
            });
        }
    }

    function doMove(id, newStage, card) {
        setError('');
        api('/tracker/' + id + '/move', { stage: newStage }).then(function (out) {
            if (!out.data || !out.data.success) {
                setError((out.data && out.data.error) || 'Move failed.');
                return;
            }
            if (card) moveCardInDom(card, newStage);
        }).catch(function () { setError('Move failed. Check your connection.'); });
    }

    /* ---------- modal ---------- */

    function openModal(card) {
        var back = document.getElementById('tkModalBack');
        var title = document.getElementById('tkModalTitle');
        document.getElementById('tkFormError').hidden = true;
        if (card) {
            title.textContent = 'Edit application';
            document.getElementById('tkId').value = card.getAttribute('data-id');
            document.getElementById('tkCompany').value = card.querySelector('h4').textContent;
            document.getElementById('tkRole').value = card.querySelector('.tk-role').textContent;
            document.getElementById('tkStage').value = card.getAttribute('data-stage');
        } else {
            title.textContent = 'Add application';
            document.getElementById('tkForm').reset();
            document.getElementById('tkId').value = '';
        }
        back.hidden = false;
        document.getElementById('tkCompany').focus();
    }

    function closeModal() {
        document.getElementById('tkModalBack').hidden = true;
    }

    function initModal() {
        var back = document.getElementById('tkModalBack');
        if (!back) return;
        document.getElementById('tkAddBtn').addEventListener('click', function () { openModal(null); });
        document.getElementById('tkCancel').addEventListener('click', closeModal);
        back.addEventListener('click', function (e) { if (e.target === back) closeModal(); });
        document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });

        document.getElementById('tkForm').addEventListener('submit', function (e) {
            e.preventDefault();
            var errBox = document.getElementById('tkFormError');
            errBox.hidden = true;
            var id = document.getElementById('tkId').value;
            var payload = {
                company: document.getElementById('tkCompany').value.trim(),
                role: document.getElementById('tkRole').value.trim(),
                stage: document.getElementById('tkStage').value,
                applied_date: document.getElementById('tkDate').value,
                jd_url: document.getElementById('tkUrl').value.trim(),
                notes: document.getElementById('tkNotes').value.trim()
            };
            if (!payload.company || !payload.role) {
                errBox.textContent = 'Company and role are required.';
                errBox.hidden = false;
                return;
            }
            var path = id ? '/tracker/' + encodeURIComponent(id) + '/edit' : '/tracker/add';
            api(path, payload).then(function (out) {
                if (out.status === 429) {
                    errBox.textContent = 'Too many requests. Please wait a moment and retry.';
                    errBox.hidden = false;
                    return;
                }
                if (!out.data || !out.data.success) {
                    errBox.textContent = (out.data && out.data.error) || 'Save failed.';
                    errBox.hidden = false;
                    return;
                }
                // Simplest correct refresh: reload the board.
                window.location.reload();
            }).catch(function () {
                errBox.textContent = 'Save failed. Check your connection.';
                errBox.hidden = false;
            });
        });
    }

    /* ---------- drag & drop ---------- */

    function initDnD() {
        document.querySelectorAll('.tk-col').forEach(function (col) {
            col.addEventListener('dragover', function (e) {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                col.classList.add('tk-drop');
            });
            col.addEventListener('dragleave', function () { col.classList.remove('tk-drop'); });
            col.addEventListener('drop', function (e) {
                e.preventDefault();
                col.classList.remove('tk-drop');
                var id = e.dataTransfer.getData('text/plain');
                var card = document.querySelector('.tk-card[data-id="' + id + '"]');
                var newStage = col.getAttribute('data-stage');
                if (!card || card.getAttribute('data-stage') === newStage) return;
                doMove(id, newStage, card);
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.tk-card').forEach(bindCard);
        initDnD();
        initModal();
    });

    window.tkCardHtml = cardHtml;
})();

/**
 * Application Tracker — Phase 1 additions (deadline setter, interview kit,
 * offer comparison, referrals, timeline, weekly digest, CSV import).
 * All user-supplied content is rendered via textContent/createElement only.
 */
(function () {
    'use strict';

    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var KIND_LABELS = { oa: 'OA', interview: 'Interview', followup: 'Follow-up', offer: 'Offer', other: 'Other' };
    // Must match the backend Referral statuses (asked|replied|referred|declined).
    var REF_STATUSES = ['asked', 'replied', 'referred', 'declined'];

    function el(tag, text, cls) {
        var node = document.createElement(tag);
        if (text !== undefined && text !== null) node.textContent = text;
        if (cls) node.className = cls;
        return node;
    }

    function clear(node) {
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    function csrfHeaders(extra) {
        var headers = extra || {};
        var tokenMeta = document.querySelector('meta[name="csrf-token"]');
        if (tokenMeta) headers['X-CSRFToken'] = tokenMeta.getAttribute('content');
        return headers;
    }

    function req(method, path, payload) {
        var opts = { method: method, headers: csrfHeaders() };
        if (payload !== undefined) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(payload);
        }
        return fetch(path, opts).then(function (resp) {
            return resp.json().catch(function () { return {}; }).then(function (data) {
                return { status: resp.status, data: data };
            });
        });
    }

    function fmtDate(iso) {
        if (!iso) return '';
        var parts = String(iso).slice(0, 10).split('-');
        if (parts.length !== 3) return String(iso);
        var y = parseInt(parts[0], 10), m = parseInt(parts[1], 10), d = parseInt(parts[2], 10);
        if (!y || !m || m < 1 || m > 12 || !d) return String(iso);
        return d + ' ' + MONTHS[m - 1] + ' ' + y;
    }

    function closestCard(node) {
        while (node && !node.classList.contains('tk-card')) node = node.parentElement;
        return node;
    }

    function setInlineError(box, msg) {
        if (!box) return;
        box.textContent = msg || '';
        box.hidden = !msg;
    }

    /* ---------- weekly digest (bonus: hidden silently on failure) ---------- */

    function loadDigest() {
        var banner = document.getElementById('tkDigest');
        if (!banner) return;
        fetch('/api/tracker/digest').then(function (r) { return r.json(); }).then(function (body) {
            var d = body && body.digest;
            if (!body || !body.success || !d) return;
            var summary = document.getElementById('tkDigestSummary');
            var actions = document.getElementById('tkDigestActions');
            clear(summary);
            clear(actions);
            var staleN = (d.stale || []).length;
            var upN = (d.upcoming_7d || []).length;
            summary.appendChild(el('strong', 'This week: '));
            summary.appendChild(document.createTextNode(
                (d.total || 0) + ' tracked · ' + (d.response_rate || 0) + '% response · ' +
                staleN + ' stale · ' + upN + ' due in the next 7 days'));
            (d.suggested_actions || []).forEach(function (a) {
                actions.appendChild(el('li', a));
            });
            banner.hidden = false;
        }).catch(function () { /* digest is a bonus: stay hidden */ });
    }

    /* ---------- per-card: deadline setter, interview kit, timeline ---------- */

    function saveDeadline(card, btn) {
        var id = card.getAttribute('data-id');
        var dateInput = card.querySelector('.tk-dl-date');
        var kindSel = card.querySelector('.tk-dl-kind');
        var errBox = card.querySelector('.tk-dl-error');
        var line = card.querySelector('.tk-dl-line');
        setInlineError(errBox, '');
        var dateVal = dateInput ? dateInput.value : '';
        var kindVal = kindSel ? kindSel.value : 'other';
        btn.disabled = true;
        req('POST', '/tracker/' + encodeURIComponent(id) + '/deadline', {
            deadline: dateVal || null, deadline_kind: kindVal
        }).then(function (out) {
            btn.disabled = false;
            if (!out.data || !out.data.success) {
                setInlineError(errBox, (out.data && out.data.error) || 'Could not save the deadline.');
                return;
            }
            if (line) {
                if (dateVal) {
                    clear(line);
                    line.appendChild(document.createTextNode('📅 Deadline ' + fmtDate(dateVal) + ' · ' + (KIND_LABELS[kindVal] || kindVal)));
                    line.hidden = false;
                } else {
                    line.hidden = true;
                }
            }
        }).catch(function () {
            btn.disabled = false;
            setInlineError(errBox, 'Could not save the deadline. Check your connection.');
        });
    }

    function showKit(appId, card, btn) {
        var panel = document.getElementById('tkKitPanel');
        var title = document.getElementById('tkKitTitle');
        var sub = document.getElementById('tkKitSub');
        var body = document.getElementById('tkKitBody');
        if (!panel) return;
        var company = card ? card.querySelector('h4').textContent : '';
        var role = card ? card.querySelector('.tk-role').textContent : '';
        clear(body);
        title.textContent = 'Interview kit';
        sub.textContent = 'Loading kit…';
        panel.hidden = false;
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        btn.disabled = true;
        req('GET', '/api/tracker/' + encodeURIComponent(appId) + '/kit').then(function (out) {
            btn.disabled = false;
            var kit = out.data && out.data.kit;
            if (!out.data || !out.data.success || !kit) {
                sub.textContent = 'Kit unavailable — try again.';
                return;
            }
            clear(sub);
            sub.appendChild(document.createTextNode((kit.company || company) + (kit.role || role ? ' · ' + (kit.role || role) : '')));
            clear(body);
            var questions = kit.questions || [];
            if (questions.length) {
                body.appendChild(el('h3', 'Practice questions'));
                var ol = el('ol', null, 'tk-kit-list');
                questions.forEach(function (q) {
                    ol.appendChild(el('li', (q && q.question) || q));
                });
                body.appendChild(ol);
            }
            if (kit.roadmap && kit.roadmap.url) {
                var p = el('p', null);
                p.appendChild(document.createTextNode('Roadmap: '));
                var a = el('a', kit.roadmap.title || kit.roadmap.url);
                var href = String(kit.roadmap.url);
                if (/^(https?:\/\/|\/)/.test(href)) a.setAttribute('href', href);
                if (/^https?:\/\//.test(href)) { a.setAttribute('target', '_blank'); a.setAttribute('rel', 'noopener'); }
                p.appendChild(a);
                body.appendChild(p);
            }
            var checklist = kit.checklist || [];
            if (checklist.length) {
                body.appendChild(el('h3', 'Checklist'));
                var ul = el('ul', null, 'tk-kit-list');
                checklist.forEach(function (c) { ul.appendChild(el('li', c)); });
                body.appendChild(ul);
            }
            if (!questions.length && !checklist.length) {
                body.appendChild(el('p', 'No kit content available for this application yet.'));
            }
        }).catch(function () {
            btn.disabled = false;
            sub.textContent = 'Kit unavailable — try again.';
        });
    }

    function toggleTimeline(card, btn) {
        var id = card.getAttribute('data-id');
        var box = card.querySelector('.tk-timeline');
        if (!box) return;
        if (!box.hidden) {
            box.hidden = true;
            btn.setAttribute('aria-expanded', 'false');
            return;
        }
        btn.setAttribute('aria-expanded', 'true');
        if (box.getAttribute('data-tk-loaded') === '1') {
            box.hidden = false;
            return;
        }
        clear(box);
        box.appendChild(el('p', 'Loading timeline…'));
        box.hidden = false;
        req('GET', '/api/tracker/' + encodeURIComponent(id) + '/timeline').then(function (out) {
            var events = out.data && out.data.timeline;
            clear(box);
            if (!out.data || !out.data.success || !events) {
                box.appendChild(el('p', 'Timeline unavailable — try again.'));
                return;
            }
            if (!events.length) {
                box.appendChild(el('p', 'No timeline events yet.'));
                box.setAttribute('data-tk-loaded', '1');
                return;
            }
            var ul = document.createElement('ul');
            events.forEach(function (ev) {
                var li = document.createElement('li');
                if (ev.date) li.appendChild(el('span', fmtDate(ev.date) + ' — ', 'tk-tl-date'));
                li.appendChild(el('strong', ev.event || 'Update'));
                if (ev.detail) li.appendChild(document.createTextNode(' — ' + ev.detail));
                ul.appendChild(li);
            });
            box.appendChild(ul);
            box.setAttribute('data-tk-loaded', '1');
        }).catch(function () {
            clear(box);
            box.appendChild(el('p', 'Timeline unavailable — try again.'));
        });
    }

    function initBoardActions() {
        var board = document.getElementById('tkBoard');
        if (!board) return;
        board.addEventListener('click', function (e) {
            var target = e.target;
            var card = closestCard(target);
            if (!card) return;
            if (target.classList.contains('tk-dl-save')) {
                e.preventDefault();
                saveDeadline(card, target);
            } else if (target.classList.contains('tk-kit-btn')) {
                e.preventDefault();
                showKit(card.getAttribute('data-id'), card, target);
            } else if (target.classList.contains('tk-timeline-btn')) {
                e.preventDefault();
                toggleTimeline(card, target);
            }
        });
        var closeBtn = document.getElementById('tkKitClose');
        if (closeBtn) closeBtn.addEventListener('click', function () {
            document.getElementById('tkKitPanel').hidden = true;
        });
    }

    /* ---------- offer comparison ---------- */

    function numVal(id, required) {
        var input = document.getElementById(id);
        if (!input) return { ok: !required, val: 0 };
        var raw = (input.value || '').trim();
        if (raw === '') return { ok: !required, val: 0 };
        var n = parseFloat(raw);
        if (isNaN(n) || n < 0) return { ok: false, val: 0 };
        return { ok: true, val: n };
    }

    function readOffer(prefix, required) {
        var titleInput = document.getElementById(prefix + 'Title');
        var title = titleInput ? titleInput.value.trim() : '';
        if (!title) return required ? { error: 'Offer title is required.' } : null;
        var base = numVal(prefix + 'Base', required);
        var bonus = numVal(prefix + 'Bonus', false);
        var esop = numVal(prefix + 'Esop', false);
        if (!base.ok) return { error: 'Base salary must be a non-negative number.' };
        if (!bonus.ok || !esop.ok) return { error: 'Bonus and ESOP must be non-negative numbers.' };
        var locInput = document.getElementById(prefix + 'Loc');
        return {
            title: title, base_lpa: base.val, bonus_lpa: bonus.val,
            esop_lpa: esop.val, location: locInput ? locInput.value.trim() : ''
        };
    }

    function initCompare() {
        var roleSel = document.getElementById('tkCmpRole');
        var otherWrap = document.getElementById('tkCmpRoleOtherWrap');
        var otherInput = document.getElementById('tkCmpRoleOther');
        if (roleSel) {
            var seen = {};
            document.querySelectorAll('.tk-card .tk-role').forEach(function (node) {
                var r = node.textContent.trim();
                if (r && !seen[r]) {
                    seen[r] = true;
                    var opt = el('option', r);
                    opt.value = r;
                    roleSel.appendChild(opt);
                }
            });
            var otherOpt = el('option', 'Other…');
            otherOpt.value = '__other__';
            roleSel.appendChild(otherOpt);
            roleSel.addEventListener('change', function () {
                if (otherWrap) otherWrap.hidden = roleSel.value !== '__other__';
            });
        }
        var addThird = document.getElementById('tkCmpAddThird');
        var third = document.getElementById('tkCmpOffer3');
        if (addThird && third) {
            addThird.addEventListener('click', function () {
                var show = third.hidden;
                third.hidden = !show;
                addThird.textContent = show ? '− Remove third offer' : '+ Add third offer';
            });
        }
        var btn = document.getElementById('tkCmpBtn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            var errBox = document.getElementById('tkCmpError');
            var results = document.getElementById('tkCmpResults');
            setInlineError(errBox, '');
            clear(results);
            var o1 = readOffer('tkCmp1', true);
            var o2 = readOffer('tkCmp2', true);
            var o3 = null;
            if (third && !third.hidden) o3 = readOffer('tkCmp3', false);
            if (o1 && o1.error) { setInlineError(errBox, 'Offer 1: ' + o1.error); return; }
            if (o2 && o2.error) { setInlineError(errBox, 'Offer 2: ' + o2.error); return; }
            if (o3 && o3.error) { setInlineError(errBox, 'Offer 3: ' + o3.error); return; }
            var offers = [o1, o2];
            if (o3) offers.push(o3);
            var role = roleSel ? roleSel.value : '';
            if (role === '__other__') role = otherInput ? otherInput.value.trim() : '';
            var tierSel = document.getElementById('tkCmpTier');
            var payload = { offers: offers };
            if (role) payload.role = role;
            if (tierSel && tierSel.value) payload.tier = tierSel.value;
            btn.disabled = true;
            req('POST', '/api/tracker/offers/compare', payload).then(function (out) {
                btn.disabled = false;
                if (!out.data || !out.data.success) {
                    setInlineError(errBox, (out.data && out.data.error) || 'Comparison failed.');
                    return;
                }
                var ranked = out.data.offers || [];
                if (!ranked.length) {
                    results.appendChild(el('p', 'No offers to compare.'));
                    return;
                }
                var table = el('table', null, 'tk-offer-table');
                var thead = document.createElement('thead');
                var hr = document.createElement('tr');
                ['Rank', 'Offer', 'Base', 'Bonus', 'ESOP', 'Total', 'Location'].forEach(function (h) {
                    hr.appendChild(el('th', h));
                });
                thead.appendChild(hr);
                table.appendChild(thead);
                var tbody = document.createElement('tbody');
                ranked.forEach(function (o, idx) {
                    var tr = document.createElement('tr');
                    if (idx === 0) tr.className = 'tk-rank-1';
                    tr.appendChild(el('td', String(o.rank !== undefined ? o.rank : idx + 1)));
                    tr.appendChild(el('td', o.title || ''));
                    tr.appendChild(el('td', String(o.base_lpa !== undefined ? o.base_lpa : '')));
                    tr.appendChild(el('td', String(o.bonus_lpa !== undefined ? o.bonus_lpa : '')));
                    tr.appendChild(el('td', String(o.esop_lpa !== undefined ? o.esop_lpa : '')));
                    tr.appendChild(el('td', String(o.total_lpa !== undefined ? o.total_lpa : '')));
                    tr.appendChild(el('td', o.location || ''));
                    tbody.appendChild(tr);
                });
                table.appendChild(tbody);
                results.appendChild(table);
                if (out.data.band) {
                    var b = out.data.band;
                    results.appendChild(el('p', 'Salary band: ' + b.min_lpa + ' – ' + b.median_lpa + ' (median) – ' + b.max_lpa + ' LPA'));
                }
                if (out.data.verdict) results.appendChild(el('p', out.data.verdict));
            }).catch(function () {
                btn.disabled = false;
                setInlineError(errBox, 'Comparison failed. Check your connection.');
            });
        });
    }

    /* ---------- referrals ---------- */

    function renderReferrals(list) {
        var wrap = document.getElementById('tkRefList');
        if (!wrap) return;
        clear(wrap);
        if (!list || !list.length) {
            wrap.appendChild(el('p', 'No referrals yet — ask someone for one.'));
            return;
        }
        list.forEach(function (r) {
            var row = el('div', null, 'tk-ref-row');
            var main = el('div', null);
            main.appendChild(el('strong', r.contact_name || 'Unnamed contact'));
            var bits = [];
            if (r.company) bits.push(r.company);
            if (r.role) bits.push(r.role);
            if (r.channel) bits.push(r.channel);
            if (bits.length) main.appendChild(document.createTextNode(' · ' + bits.join(' — ')));
            if (r.notes) main.appendChild(el('div', r.notes));
            row.appendChild(main);
            if (r.status) row.appendChild(el('span', r.status, 'tk-pill'));
            var sel = document.createElement('select');
            sel.setAttribute('aria-label', 'Referral status');
            sel.className = 'tk-ref-status';
            sel.setAttribute('data-ref-id', r.id);
            var opts = REF_STATUSES.slice();
            if (r.status && opts.indexOf(r.status) === -1) opts.push(r.status);
            opts.forEach(function (s) {
                var o = el('option', s);
                o.value = s;
                if (s === r.status) o.selected = true;
                sel.appendChild(o);
            });
            sel.addEventListener('change', function () {
                var prev = r.status;
                req('PATCH', '/api/tracker/referrals/' + encodeURIComponent(r.id), { status: sel.value }).then(function (out) {
                    if (!out.data || (!out.data.referral && !out.data.success)) {
                        sel.value = prev || REF_STATUSES[0];
                        setInlineError(document.getElementById('tkRefError'), (out.data && out.data.error) || 'Status update failed.');
                        return;
                    }
                    loadReferrals();
                }).catch(function () {
                    sel.value = prev || REF_STATUSES[0];
                    setInlineError(document.getElementById('tkRefError'), 'Status update failed. Check your connection.');
                });
            });
            row.appendChild(sel);
            var del = el('button', 'Delete', 'tk-mini danger');
            del.type = 'button';
            del.addEventListener('click', function () {
                if (!window.confirm('Delete this referral?')) return;
                req('DELETE', '/api/tracker/referrals/' + encodeURIComponent(r.id)).then(function (out) {
                    if (!out.data || !out.data.success) {
                        setInlineError(document.getElementById('tkRefError'), (out.data && out.data.error) || 'Delete failed.');
                        return;
                    }
                    loadReferrals();
                }).catch(function () {
                    setInlineError(document.getElementById('tkRefError'), 'Delete failed. Check your connection.');
                });
            });
            row.appendChild(del);
            wrap.appendChild(row);
        });
    }

    function loadReferrals() {
        var wrap = document.getElementById('tkRefList');
        if (!wrap) return;
        fetch('/api/tracker/referrals').then(function (r) { return r.json(); }).then(function (body) {
            renderReferrals((body && body.referrals) || []);
        }).catch(function () {
            clear(wrap);
            wrap.appendChild(el('p', 'Referrals unavailable — try again.'));
        });
    }

    function initReferrals() {
        var appSel = document.getElementById('tkRefApp');
        if (appSel) {
            document.querySelectorAll('.tk-card').forEach(function (card) {
                var company = card.querySelector('h4');
                var role = card.querySelector('.tk-role');
                var opt = el('option', (company ? company.textContent : '') + (role ? ' — ' + role.textContent : ''));
                opt.value = card.getAttribute('data-id');
                appSel.appendChild(opt);
            });
        }
        var form = document.getElementById('tkRefForm');
        if (!form) return;
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            var errBox = document.getElementById('tkRefError');
            setInlineError(errBox, '');
            var nameInput = document.getElementById('tkRefName');
            var name = nameInput ? nameInput.value.trim() : '';
            if (!name) {
                setInlineError(errBox, 'Contact name is required.');
                return;
            }
            var payload = { contact_name: name };
            if (appSel && appSel.value) payload.application_id = parseInt(appSel.value, 10);
            var channelSel = document.getElementById('tkRefChannel');
            if (channelSel && channelSel.value) payload.channel = channelSel.value;
            var notesInput = document.getElementById('tkRefNotes');
            if (notesInput && notesInput.value.trim()) payload.notes = notesInput.value.trim();
            req('POST', '/api/tracker/referrals', payload).then(function (out) {
                if (out.status !== 201 && (!out.data || !out.data.referral)) {
                    setInlineError(errBox, (out.data && out.data.error) || 'Could not add the referral.');
                    return;
                }
                form.reset();
                loadReferrals();
            }).catch(function () {
                setInlineError(errBox, 'Could not add the referral. Check your connection.');
            });
        });
        loadReferrals();
    }

    /* ---------- CSV import ---------- */

    function initCsvImport() {
        var btn = document.getElementById('tkCsvBtn');
        var fileInput = document.getElementById('tkCsvFile');
        var result = document.getElementById('tkCsvResult');
        if (!btn || !fileInput || !result) return;
        btn.addEventListener('click', function () {
            clear(result);
            var file = fileInput.files && fileInput.files[0];
            if (!file) {
                result.appendChild(el('p', 'Choose a CSV file first.', 'tk-error'));
                return;
            }
            var reader = new FileReader();
            reader.onload = function () {
                btn.disabled = true;
                req('POST', '/api/tracker/import', { csv: reader.result }).then(function (out) {
                    btn.disabled = false;
                    if (!out.data || !out.data.success) {
                        result.appendChild(el('p', (out.data && out.data.error) || 'Import failed.', 'tk-error'));
                        return;
                    }
                    var d = out.data;
                    result.appendChild(el('p', 'Imported ' + (d.imported || 0) + ', skipped ' + (d.skipped || 0) + '.'));
                    (d.errors || []).forEach(function (err) {
                        result.appendChild(el('p', 'Row ' + (err.row !== undefined ? err.row : '?') + ': ' + (err.error || 'skipped')));
                    });
                    if (d.imported > 0) {
                        result.appendChild(el('p', 'Reloading board…'));
                        setTimeout(function () { window.location.reload(); }, 2000);
                    }
                }).catch(function () {
                    btn.disabled = false;
                    result.appendChild(el('p', 'Import failed. Check your connection.', 'tk-error'));
                });
            };
            reader.onerror = function () {
                result.appendChild(el('p', 'Could not read that file.', 'tk-error'));
            };
            reader.readAsText(file);
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        loadDigest();
        initBoardActions();
        initCompare();
        initReferrals();
        initCsvImport();
    });
})();
