/* India Salary Explorer (Wave 1 #8) — XSS-safe rendering via textContent only. */
(function () {
    'use strict';
    var opts = window.SALARY_OPTIONS || {};
    var authed = !!window.SALARY_AUTHED;
    var roles = opts.roles || [];
    var tiers = opts.tiers || {};
    var bands = opts.experience_bands || [];
    var meta = opts.meta || {};

    var fRole = document.getElementById('fRole');
    var fTier = document.getElementById('fTier');
    var fBand = document.getElementById('fBand');
    var grid = document.getElementById('salGrid');
    var countEl = document.getElementById('salCount');
    var compareVal = null;

    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }

    function fillSelect(sel, items, allLabel) {
        sel.textContent = '';
        var all = document.createElement('option');
        all.value = '';
        all.textContent = allLabel;
        sel.appendChild(all);
        items.forEach(function (it) {
            var o = document.createElement('option');
            o.value = it.value;
            o.textContent = it.label;
            sel.appendChild(o);
        });
    }

    fillSelect(fRole, roles.map(function (r) { return { value: r, label: r }; }), 'All roles');
    fillSelect(fTier, Object.keys(tiers).map(function (k) {
        return { value: k, label: tiers[k].label + ' — ' + tiers[k].cities.join(', ') };
    }), 'All tiers');
    fillSelect(fBand, bands.map(function (b) { return { value: b, label: b + ' yrs' }; }), 'All experience');

    function fmt(n) { return '₹' + n + ' LPA'; }

    function renderCard(e) {
        var card = el('div', 'sal-card b-card b-card--lite');
        card.appendChild(el('h3', null, e.role));
        card.appendChild(el('div', 'sal-sub',
            e.tier_label + ' · ' + e.cities.join(', ') + ' · ' + e.experience_band + ' yrs exp'));

        // Range bar: pad the domain a little so markers never sit on the edge.
        var lo = e.min_lpa, md = e.median_lpa, hi = e.max_lpa;
        var pad = Math.max((hi - lo) * 0.08, 0.2);
        var dMin = Math.max(0, lo - pad), dMax = hi + pad;
        var pct = function (v) { return ((v - dMin) / (dMax - dMin)) * 100; };

        var bar = el('div', 'sal-bar');
        bar.setAttribute('role', 'img');
        bar.setAttribute('aria-label', 'Salary range ' + fmt(lo) + ' to ' + fmt(hi) + ', median ' + fmt(md));
        var fill = el('div', 'fill');
        fill.style.left = pct(lo) + '%';
        fill.style.width = (pct(hi) - pct(lo)) + '%';
        bar.appendChild(fill);
        var med = el('div', 'med');
        med.style.left = 'calc(' + pct(md) + '% - 1.5px)';
        med.title = 'Median ' + fmt(md);
        bar.appendChild(med);
        if (compareVal !== null) {
            var c = Math.min(Math.max(compareVal, dMin), dMax);
            var you = el('div', 'you');
            you.style.left = 'calc(' + pct(c) + '% - 1px)';
            you.title = 'Your expected: ' + fmt(compareVal);
            bar.appendChild(you);
        }
        card.appendChild(bar);

        var nums = el('div', 'sal-nums');
        nums.appendChild(el('span', 'lo', 'Min ' + fmt(lo)));
        nums.appendChild(el('span', 'md', 'Median ' + fmt(md)));
        nums.appendChild(el('span', 'hi', 'Max ' + fmt(hi)));
        card.appendChild(nums);
        return card;
    }

    function render(entries) {
        grid.textContent = '';
        if (!entries.length) {
            grid.appendChild(el('div', 'sal-empty', 'No ranges match these filters.'));
        } else {
            entries.forEach(function (e) { grid.appendChild(renderCard(e)); });
        }
        countEl.textContent = entries.length
            ? 'Showing ' + entries.length + ' indicative range' + (entries.length === 1 ? '' : 's') + '.'
            : '';
    }

    function load() {
        var q = new URLSearchParams();
        if (fRole.value) q.set('role', fRole.value);
        if (fTier.value) q.set('tier', fTier.value);
        if (fBand.value) q.set('band', fBand.value);
        fetch('/salary/data?' + q.toString(), { headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.json(); })
            .then(function (body) {
                if (!body || !body.success) { render([]); return; }
                render(body.entries || []);
            })
            .catch(function () { render([]); });
    }

    [fRole, fTier, fBand].forEach(function (s) {
        s.addEventListener('change', load);
    });

    if (authed) {
        var cmpInput = document.getElementById('cmpVal');
        document.getElementById('cmpBtn').addEventListener('click', function () {
            var v = parseFloat(cmpInput.value);
            if (isNaN(v) || v <= 0 || v > 200) {
                cmpInput.style.borderColor = 'var(--accent-danger)';
                return;
            }
            cmpInput.style.borderColor = '';
            compareVal = Math.round(v * 2) / 2;
            load();
        });
        document.getElementById('cmpClear').addEventListener('click', function () {
            compareVal = null;
            cmpInput.value = '';
            load();
        });
    }

    // Disclaimer note is rendered server-side in the template.

    load();
})();

/* Phase 2 — growth curve, offer evaluator, negotiation scripts, city toggle.
 * All rendering via textContent/createElement (XSS-safe). Every API call
 * degrades to an inline error message; nothing here touches the explorer above. */
(function () {
    'use strict';
    var opts = window.SALARY_OPTIONS || {};
    var authed = !!window.SALARY_AUTHED;
    var roles = opts.roles || [];
    var tiers = opts.tiers || {};
    var bands = opts.experience_bands || [];
    var INDICATIVE = 'Indicative ranges — curated for student guidance, not live market data.';

    function $(id) { return document.getElementById(id); }
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }
    function fmt(n) { return '₹' + n + ' LPA'; }
    function csrfHeaders(extra) {
        var headers = extra || {};
        var tokenMeta = document.querySelector('meta[name="csrf-token"]');
        if (tokenMeta) headers['X-CSRFToken'] = tokenMeta.getAttribute('content');
        return headers;
    }
    function fillSelect(sel, items, allLabel) {
        if (!sel) return;
        sel.textContent = '';
        if (allLabel) {
            var all = document.createElement('option');
            all.value = '';
            all.textContent = allLabel;
            sel.appendChild(all);
        }
        items.forEach(function (it) {
            var o = document.createElement('option');
            o.value = it.value;
            o.textContent = it.label;
            sel.appendChild(o);
        });
    }
    function fetchJson(url, init) {
        return fetch(url, init).then(function (r) {
            return r.json().then(function (data) {
                return { status: r.status, data: data };
            });
        });
    }
    function setStatus(node, msg, isErr) {
        if (!node) return;
        node.textContent = msg || '';
        node.className = 'sal-status' + (isErr ? ' err' : '');
    }

    /* ---------- Growth curve ---------- */
    var curveRole = $('salCurveRole'), curveTier = $('salCurveTier');
    if (curveRole && curveTier) {
        fillSelect(curveRole, roles.map(function (r) { return { value: r, label: r }; }), 'Choose a role');
        fillSelect(curveTier, Object.keys(tiers).map(function (k) {
            return { value: k, label: tiers[k].label };
        }), 'All tiers');
        // Preselect from the explorer's current filters, and keep in sync.
        var syncCurve = function () {
            var fr = $('fRole'), ft = $('fTier');
            if (fr && fr.value) curveRole.value = fr.value;
            if (ft && ft.value) curveTier.value = ft.value;
        };
        syncCurve();
        var frEl = $('fRole'), ftEl = $('fTier');
        if (frEl) frEl.addEventListener('change', syncCurve);
        if (ftEl) ftEl.addEventListener('change', syncCurve);
        $('salCurveBtn').addEventListener('click', function () {
            var status = $('salCurveStatus'), out = $('salCurveResults');
            if (!curveRole.value) {
                setStatus(status, 'Choose a role to see its growth curve.', true);
                return;
            }
            setStatus(status, 'Loading curve…');
            out.textContent = '';
            var q = new URLSearchParams({ role: curveRole.value });
            if (curveTier.value) q.set('tier', curveTier.value);
            fetchJson('/api/salary/curve?' + q.toString())
                .then(function (res) {
                    var body = res.data;
                    if (res.status !== 200 || !body || !body.success || !(body.curve || []).length) {
                        setStatus(status, (body && body.error) || 'No growth curve available for this role and tier.', true);
                        return;
                    }
                    setStatus(status, '');
                    var table = el('table', 'sal-table');
                    var thead = el('thead');
                    var hr = el('tr');
                    ['Experience', 'Min', 'Median', 'Max'].forEach(function (h) {
                        hr.appendChild(el('th', null, h));
                    });
                    thead.appendChild(hr);
                    table.appendChild(thead);
                    var tbody = el('tbody');
                    body.curve.forEach(function (row) {
                        var tr = el('tr');
                        tr.appendChild(el('td', null, row.band));
                        tr.appendChild(el('td', 'num', fmt(row.min_lpa)));
                        tr.appendChild(el('td', 'num', fmt(row.median_lpa)));
                        tr.appendChild(el('td', 'num', fmt(row.max_lpa)));
                        tbody.appendChild(tr);
                    });
                    table.appendChild(tbody);
                    out.appendChild(table);
                    out.appendChild(el('p', 'sal-status', body.disclaimer || INDICATIVE));
                })
                .catch(function () {
                    setStatus(status, 'Could not load the growth curve. Try again.', true);
                });
        });
    }

    /* ---------- Offer evaluator (authed only; form exists only then) ---------- */
    var evalForm = $('salEvalForm');
    if (evalForm && authed) {
        fillSelect($('salEvalRole'), roles.map(function (r) { return { value: r, label: r }; }));
        fillSelect($('salEvalTier'), Object.keys(tiers).map(function (k) {
            return { value: k, label: tiers[k].label };
        }));
        fillSelect($('salEvalBand'), bands.map(function (b) { return { value: b, label: b + ' yrs' }; }));
        var cities = [];
        Object.keys(tiers).forEach(function (k) {
            (tiers[k].cities || []).forEach(function (c) {
                if (cities.indexOf(c) === -1) cities.push(c);
            });
        });
        cities.sort();
        fillSelect($('salEvalCity'), cities.map(function (c) { return { value: c, label: c }; }), 'Any city');
        var scriptsCache = null;
        var scriptsLoading = null;
        function loadScripts() {
            if (scriptsCache) return Promise.resolve(scriptsCache);
            if (!scriptsLoading) {
                scriptsLoading = fetchJson('/api/salary/scripts')
                    .then(function (res) {
                        scriptsCache = (res.data && res.data.scripts) || [];
                        return scriptsCache;
                    })
                    .catch(function () { return []; });
            }
            return scriptsLoading;
        }
        evalForm.addEventListener('submit', function (ev) {
            ev.preventDefault();
            var status = $('salEvalStatus'), out = $('salEvalResults');
            var role = $('salEvalRole').value, tier = $('salEvalTier').value,
                band = $('salEvalBand').value, city = $('salEvalCity').value;
            var offer = parseFloat($('salEvalOffer').value);
            if (!role || !tier || !band || isNaN(offer) || offer < 0) {
                setStatus(status, 'Choose a role, tier and experience band, and enter an offer of 0 LPA or more.', true);
                return;
            }
            setStatus(status, 'Evaluating…');
            out.textContent = '';
            var payload = { role: role, tier: tier, band: band, offer_lpa: offer };
            if (city) payload.city = city;
            fetchJson('/api/salary/evaluate', {
                method: 'POST',
                headers: csrfHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify(payload)
            })
                .then(function (res) {
                    var body = res.data;
                    if (res.status !== 200 || !body || !body.success || !body.evaluation) {
                        setStatus(status, (body && body.error) || 'Could not evaluate this offer.', true);
                        return;
                    }
                    setStatus(status, '');
                    var ev = body.evaluation;
                    // Contract names the band field `band`; it may be an
                    // object with the numbers or the band label itself.
                    var bandObj = (ev.band && typeof ev.band === 'object') ? ev.band : ev;
                    if (bandObj.min_lpa != null && bandObj.max_lpa != null) {
                        out.appendChild(el('p', 'sal-eval-line',
                            'Your offer: ' + fmt(ev.offer_lpa) + ' · Band: ' + fmt(bandObj.min_lpa) + ' – ' + fmt(bandObj.max_lpa)
                            + (bandObj.median_lpa != null ? ' (median ' + fmt(bandObj.median_lpa) + ')' : '') + '.'));
                    } else {
                        out.appendChild(el('p', 'sal-eval-line', 'Your offer: ' + fmt(ev.offer_lpa) + '.'));
                    }
                    if (ev.percentile_note) out.appendChild(el('p', 'sal-eval-line', ev.percentile_note));
                    if (ev.city_adjusted) {
                        out.appendChild(el('p', 'sal-eval-line',
                            'City-adjusted median for ' + ev.city_adjusted.city + ': ' + fmt(ev.city_adjusted.adjusted_median)
                            + ' (multiplier ' + ev.city_adjusted.multiplier + '×).'));
                    }
                    if ((ev.suggested_scripts || []).length) {
                        out.appendChild(el('p', 'sal-eval-line', 'Suggested scripts:'));
                        ev.suggested_scripts.forEach(function (sid) {
                            var btn = el('button', 'sal-script-btn', 'Loading script…');
                            btn.type = 'button';
                            var wrap = el('div');
                            wrap.appendChild(btn);
                            out.appendChild(wrap);
                            loadScripts().then(function (scripts) {
                                var found = null;
                                scripts.forEach(function (s) {
                                    if (String(s.id) === String(sid)) found = s;
                                });
                                if (found) {
                                    btn.textContent = found.title;
                                    btn.addEventListener('click', function () {
                                        var detail = btn.nextElementSibling;
                                        if (detail) {
                                            detail.hidden = !detail.hidden;
                                            return;
                                        }
                                        var d = el('div');
                                        d.appendChild(el('p', null, found.script));
                                        if (found.tip) d.appendChild(el('p', 'sal-script-tip', 'Tip: ' + found.tip));
                                        wrap.appendChild(d);
                                    });
                                } else {
                                    btn.textContent = 'Script ' + sid;
                                    btn.disabled = true;
                                }
                            });
                        });
                    }
                    out.appendChild(el('p', 'sal-status', body.disclaimer || INDICATIVE));
                })
                .catch(function () {
                    setStatus(status, 'Could not evaluate this offer. Try again.', true);
                });
        });
    }

    /* ---------- Negotiation scripts browser ---------- */
    var scriptSel = $('salScriptSituation'), scriptList = $('salScriptList');
    if (scriptSel && scriptList) {
        var situations = [
            { value: 'below-band', label: 'Offer below the band' },
            { value: 'at-band', label: 'Offer within the band' },
            { value: 'above-band', label: 'Offer above the band' },
            { value: 'competing-offer', label: 'Competing offer in hand' },
            { value: 'first-offer', label: 'First offer' },
            { value: 'intern-conversion', label: 'Intern to full-time conversion' }
        ];
        fillSelect(scriptSel, situations, 'All situations');
        var loadBrowser = function () {
            var status = $('salScriptStatus');
            setStatus(status, 'Loading scripts…');
            scriptList.textContent = '';
            var q = scriptSel.value ? new URLSearchParams({ situation: scriptSel.value }) : '';
            fetchJson('/api/salary/scripts' + (q ? '?' + q.toString() : ''))
                .then(function (res) {
                    var body = res.data;
                    if (res.status !== 200 || !body || !body.success) {
                        setStatus(status, (body && body.error) || 'Could not load scripts. Try again.', true);
                        return;
                    }
                    var scripts = body.scripts || [];
                    if (!scripts.length) {
                        setStatus(status, 'No scripts for this situation.');
                        return;
                    }
                    setStatus(status, '');
                    scripts.forEach(function (s) {
                        var card = el('div', 'sal-card b-card b-card--lite sal-script-card');
                        card.appendChild(el('h3', null, s.title));
                        card.appendChild(el('p', null, s.script));
                        if (s.tip) card.appendChild(el('p', 'sal-script-tip', 'Tip: ' + s.tip));
                        scriptList.appendChild(card);
                    });
                })
                .catch(function () {
                    setStatus(status, 'Could not load scripts. Try again.', true);
                });
        };
        scriptSel.addEventListener('change', loadBrowser);
        loadBrowser();
    }

    /* ---------- City comparison toggle ---------- */
    var cityToggle = $('salCityToggle');
    if (cityToggle) {
        var expanded = false;
        cityToggle.addEventListener('click', function () {
            expanded = !expanded;
            var total = document.querySelectorAll('.sal-city-extra').length;
            Array.prototype.forEach.call(document.querySelectorAll('.sal-city-extra'), function (row) {
                row.hidden = !expanded;
            });
            cityToggle.textContent = expanded ? 'Show fewer cities' : 'Show all cities (' + total + ')';
        });
    }
})();
