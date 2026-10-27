/* Portfolio builder (Wave 1 #12) — XSS-safe: textContent only, no innerHTML. */
(function () {
    'use strict';
    var existing = window.PORTFOLIO_EXISTING || null;
    var tokenMeta = document.querySelector('meta[name="csrf-token"]');

    var fName = document.getElementById('fName');
    var fHeadline = document.getElementById('fHeadline');
    var fBio = document.getElementById('fBio');
    var fSkills = document.getElementById('fSkills');
    var projRows = document.getElementById('projRows');
    var linkRows = document.getElementById('linkRows');
    var certRows = document.getElementById('certRows');
    var expRows = document.getElementById('expRows');
    var eduRows = document.getElementById('eduRows');
    var achRows = document.getElementById('achRows');
    var fContactEmail = document.getElementById('fContactEmail');
    var fContactPhone = document.getElementById('fContactPhone');
    var fContactLocation = document.getElementById('fContactLocation');
    var saveBtn = document.getElementById('saveBtn');
    var msg = document.getElementById('pbMsg');

    function postJSON(url, body) {
        var headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
        if (tokenMeta) headers['X-CSRFToken'] = tokenMeta.getAttribute('content');
        return fetch(url, { method: 'POST', headers: headers, body: JSON.stringify(body) })
            .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); });
    }

    function getJSON(url) {
        var headers = { 'Accept': 'application/json' };
        if (tokenMeta) headers['X-CSRFToken'] = tokenMeta.getAttribute('content');
        return fetch(url, { headers: headers })
            .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); });
    }

    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }

    function inputRow(fields, values) {
        var row = el('div', 'pb-row' + (fields.length === 3 ? ' pb-row3' : ''));
        var inputs = [];
        fields.forEach(function (f, i) {
            var inp = document.createElement('input');
            inp.type = 'text';
            inp.placeholder = f;
            inp.maxLength = i === 0 ? 80 : 500;
            if (values && values[i]) inp.value = values[i];
            inp.addEventListener('input', renderPreview);
            row.appendChild(inp);
            inputs.push(inp);
        });
        var del = el('button', 'pb-mini', '✕');
        del.type = 'button';
        del.setAttribute('aria-label', 'Remove row');
        del.addEventListener('click', function () { row.remove(); renderPreview(); });
        row.appendChild(del);
        return { row: row, inputs: inputs };
    }

    var projInputs = [];
    var linkInputs = [];
    var certInputs = [];
    var expInputs = [];
    var eduInputs = [];
    var achInputs = [];

    function addProject(values) {
        var r = inputRow(['Title', 'Description', 'Link (https://…)'], values);
        projRows.appendChild(r.row);
        projInputs.push(r.inputs);
    }
    function addLink(values) {
        var r = inputRow(['Label', 'URL (https://…)'], values);
        linkRows.appendChild(r.row);
        linkInputs.push(r.inputs);
    }
    function addCert(values) {
        var r = inputRow(['Title', 'Issuer', 'Year'], values);
        certRows.appendChild(r.row);
        certInputs.push(r.inputs);
    }
    function addExp(values) {
        var r = inputRow(['Role', 'Organization', 'Period'], values);
        expRows.appendChild(r.row);
        expInputs.push(r.inputs);
    }
    function addEdu(values) {
        var r = inputRow(['Institution', 'Degree', 'Period'], values);
        eduRows.appendChild(r.row);
        eduInputs.push(r.inputs);
    }
    function addAch(values) {
        var r = inputRow(['Title', 'Description'], values);
        achRows.appendChild(r.row);
        achInputs.push(r.inputs);
    }

    document.getElementById('addProj').addEventListener('click', function () { addProject(); });
    document.getElementById('addLink').addEventListener('click', function () { addLink(); });
    document.getElementById('addCert').addEventListener('click', function () { addCert(); });
    document.getElementById('addExp').addEventListener('click', function () { addExp(); });
    document.getElementById('addEdu').addEventListener('click', function () { addEdu(); });
    document.getElementById('addAch').addEventListener('click', function () { addAch(); });

    if (existing) {
        (existing.projects || []).forEach(function (p) { addProject([p.title, p.description, p.link || '']); });
        (existing.links || []).forEach(function (l) { addLink([l.label, l.url]); });
        (existing.certifications || []).forEach(function (c) { addCert([c.title, c.issuer, c.year || '']); });
        (existing.experience || []).forEach(function (e) { addExp([e.title, e.organization, e.period || '']); });
        (existing.education || []).forEach(function (d) { addEdu([d.institution, d.degree, d.period || '']); });
        (existing.achievements || []).forEach(function (a) { addAch([a.title, a.description || '']); });
        if (existing.contact) {
            // Stored as [{label, value}]; map back onto the three fields.
            var byLabel = {};
            (Array.isArray(existing.contact) ? existing.contact : []).forEach(function (c) {
                if (c && c.label) byLabel[c.label] = c.value || '';
            });
            if (fContactEmail) fContactEmail.value = byLabel.Email || '';
            if (fContactPhone) fContactPhone.value = byLabel.Phone || '';
            if (fContactLocation) fContactLocation.value = byLabel.Location || '';
        }
    }
    if (!projInputs.length) addProject();
    if (!linkInputs.length) addLink();

    function collect() {
        var projects = projInputs
            .map(function (ins) { return { title: ins[0].value.trim(), description: ins[1].value.trim(), link: ins[2].value.trim() }; })
            .filter(function (p) { return p.title || p.description || p.link; });
        var links = linkInputs
            .map(function (ins) { return { label: ins[0].value.trim(), url: ins[1].value.trim() }; })
            .filter(function (l) { return l.label || l.url; });
        var certifications = certInputs
            .map(function (ins) { return { title: ins[0].value.trim(), issuer: ins[1].value.trim(), year: ins[2].value.trim() }; })
            .filter(function (c) { return c.title || c.issuer || c.year; });
        var experience = expInputs
            .map(function (ins) { return { title: ins[0].value.trim(), organization: ins[1].value.trim(), period: ins[2].value.trim() }; })
            .filter(function (e) { return e.title || e.organization || e.period; });
        var education = eduInputs
            .map(function (ins) { return { institution: ins[0].value.trim(), degree: ins[1].value.trim(), period: ins[2].value.trim() }; })
            .filter(function (d) { return d.institution || d.degree || d.period; });
        var achievements = achInputs
            .map(function (ins) { return { title: ins[0].value.trim(), description: ins[1].value.trim() }; })
            .filter(function (a) { return a.title || a.description; });
        var gh = document.getElementById('fGithub');
        var lc = document.getElementById('fLeetcode');
        return {
            display_name: fName.value.trim(),
            headline: fHeadline.value.trim(),
            bio: fBio.value.trim(),
            skills: fSkills.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean),
            projects: projects,
            links: links,
            certifications: certifications,
            experience: experience,
            education: education,
            achievements: achievements,
            // Backend validator stores contact as a [{label, value}] list,
            // so send that shape (skip empty fields entirely).
            contact: [
                { label: 'Email', value: fContactEmail ? fContactEmail.value.trim() : '' },
                { label: 'Phone', value: fContactPhone ? fContactPhone.value.trim() : '' },
                { label: 'Location', value: fContactLocation ? fContactLocation.value.trim() : '' },
            ].filter(function (c) { return c.value; }),
            show_github: gh ? gh.checked : false,
            show_leetcode: lc ? lc.checked : false,
            is_published: document.getElementById('fPublish').checked,
        };
    }

    // ---- live preview ----
    var pvName = document.getElementById('pvName');
    var pvHeadline = document.getElementById('pvHeadline');
    var pvBio = document.getElementById('pvBio');
    var pvSkills = document.getElementById('pvSkills');
    var pvProjects = document.getElementById('pvProjects');
    var pvLinks = document.getElementById('pvLinks');

    function renderPreview() {
        var d = collect();
        pvName.textContent = d.display_name || 'Your name';
        pvHeadline.textContent = d.headline;
        pvBio.textContent = d.bio;
        pvSkills.textContent = '';
        if (!d.skills.length) pvSkills.appendChild(el('span', 'pv-empty', 'Add skills to see them here.'));
        d.skills.forEach(function (s) { pvSkills.appendChild(el('span', 'pv-chip', s)); });
        pvProjects.textContent = '';
        if (!d.projects.length) pvProjects.appendChild(el('p', 'pv-empty', 'Add projects to see them here.'));
        d.projects.forEach(function (p) {
            var card = el('div', 'pv-proj');
            card.appendChild(el('h3', null, p.title || 'Untitled project'));
            if (p.description) card.appendChild(el('p', null, p.description));
            if (p.link) card.appendChild(el('span', 'pv-plink', p.link));
            pvProjects.appendChild(card);
        });
        pvLinks.textContent = '';
        d.links.forEach(function (l) {
            if (l.label) pvLinks.appendChild(el('span', null, l.label));
        });
    }

    [fName, fHeadline, fBio, fSkills].forEach(function (i) {
        i.addEventListener('input', renderPreview);
    });

    function setMsg(text, ok) {
        msg.textContent = text;
        msg.className = 'pb-msg ' + (ok ? 'ok' : 'err');
    }

    saveBtn.addEventListener('click', function () {
        var headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
        if (tokenMeta) headers['X-CSRFToken'] = tokenMeta.getAttribute('content');
        saveBtn.disabled = true;
        setMsg('Saving…', true);
        fetch('/portfolio/save', { method: 'POST', headers: headers, body: JSON.stringify(collect()) })
            .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
            .then(function (res) {
                saveBtn.disabled = false;
                if (!res.body || !res.body.success) {
                    setMsg((res.body && res.body.error) || 'Could not save. Please try again.', false);
                    return;
                }
                setMsg('Saved!' + (res.body.is_published ? ' Your portfolio is live.' : ' Publish it to share the link.'), true);
                var wrap = document.getElementById('pubLinkWrap');
                var link = document.getElementById('pubLink');
                if (wrap && link && res.body.public_url) {
                    wrap.style.display = '';
                    link.href = res.body.public_url;
                    link.textContent = res.body.public_url;
                }
            })
            .catch(function () {
                saveBtn.disabled = false;
                setMsg('Network error. Please try again.', false);
            });
    });

    /* ---- Phase 3: theme picker (POST immediately, small confirmation) ---- */
    (function themePicker() {
        var themeMsg = document.getElementById('themeMsg');
        var radios = document.querySelectorAll('input[name="pfTheme"]');
        if (!radios.length) return;
        var current = null;
        radios.forEach(function (r) { if (r.checked) current = r.value; });
        radios.forEach(function (r) {
            r.addEventListener('change', function () {
                if (!r.checked) return;
                var prev = current;
                current = r.value;
                postJSON('/api/portfolio/theme', { theme: r.value })
                    .then(function (res) {
                        if (res.body && res.body.success) {
                            if (themeMsg) { themeMsg.textContent = 'Theme saved'; themeMsg.className = 'pb-msg ok'; }
                        } else {
                            current = prev;
                            radios.forEach(function (x) { x.checked = (x.value === prev); });
                            if (themeMsg) { themeMsg.textContent = (res.body && res.body.error) || 'Could not save theme.'; themeMsg.className = 'pb-msg err'; }
                        }
                    })
                    .catch(function () {
                        current = prev;
                        radios.forEach(function (x) { x.checked = (x.value === prev); });
                        if (themeMsg) { themeMsg.textContent = 'Network error. Theme not saved.'; themeMsg.className = 'pb-msg err'; }
                    });
            });
        });
    })();

    /* ---- Phase 3: GitHub import (preview checkboxes -> save selected) ---- */
    (function githubImport() {
        var userInput = document.getElementById('ghUser');
        var fetchBtn = document.getElementById('ghFetchBtn');
        var results = document.getElementById('ghResults');
        var importBtn = document.getElementById('ghImportBtn');
        var ghMsg = document.getElementById('ghMsg');
        if (!userInput || !fetchBtn || !results) return;
        var preview = [];

        function setGhMsg(text, ok) {
            if (!ghMsg) return;
            ghMsg.textContent = text;
            ghMsg.className = 'pb-msg ' + (ok ? 'ok' : 'err');
        }

        fetchBtn.addEventListener('click', function () {
            var username = userInput.value.trim();
            if (!username) { setGhMsg('Enter a GitHub username first.', false); return; }
            fetchBtn.disabled = true;
            setGhMsg('Fetching repos…', true);
            postJSON('/api/portfolio/github-import', { username: username })
                .then(function (res) {
                    fetchBtn.disabled = false;
                    if (!res.body || !res.body.success || !Array.isArray(res.body.preview)) {
                        setGhMsg((res.body && res.body.error) || 'Could not fetch repos.', false);
                        return;
                    }
                    preview = res.body.preview;
                    results.textContent = '';
                    if (!preview.length) {
                        setGhMsg('No public repos found for this user.', true);
                        importBtn.hidden = true;
                        return;
                    }
                    preview.forEach(function (repo, i) {
                        var item = el('label', 'pb-gh-item');
                        var cb = document.createElement('input');
                        cb.type = 'checkbox';
                        cb.checked = true;
                        cb.setAttribute('data-gh-idx', String(i));
                        item.appendChild(cb);
                        var span = document.createElement('span');
                        span.appendChild(el('strong', null, repo.name || ''));
                        var meta = [];
                        if (repo.description) meta.push(repo.description);
                        if (typeof repo.stars === 'number') meta.push('★ ' + repo.stars);
                        if (repo.language) meta.push(repo.language);
                        if (meta.length) span.appendChild(el('small', null, ' — ' + meta.join(' · ')));
                        item.appendChild(span);
                        results.appendChild(item);
                    });
                    importBtn.hidden = false;
                    setGhMsg(preview.length + ' repos found — uncheck any you do not want.', true);
                })
                .catch(function () {
                    fetchBtn.disabled = false;
                    setGhMsg('Network error. Please try again.', false);
                });
        });

        if (importBtn) importBtn.addEventListener('click', function () {
            var checked = [];
            results.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
                if (cb.checked) checked.push(preview[parseInt(cb.getAttribute('data-gh-idx'), 10)]);
            });
            if (!checked.length) { setGhMsg('Select at least one repo.', false); return; }
            importBtn.disabled = true;
            setGhMsg('Importing…', true);
            postJSON('/api/portfolio/github-import/save', { repos: checked })
                .then(function (res) {
                    importBtn.disabled = false;
                    if (!res.body || !res.body.success) {
                        setGhMsg((res.body && res.body.error) || 'Could not import.', false);
                        return;
                    }
                    setGhMsg('Added ' + (res.body.added || checked.length) + ' projects. Reloading…', true);
                    setTimeout(function () { window.location.reload(); }, 1200);
                })
                .catch(function () {
                    importBtn.disabled = false;
                    setGhMsg('Network error. Please try again.', false);
                });
        });
    })();

    /* ---- Phase 3: analytics + review (hide silently on failure / no portfolio) ---- */
    (function analytics() {
        var block = document.getElementById('pfAnalytics');
        if (!block) return;
        getJSON('/api/portfolio/analytics').then(function (res) {
            var b = res.body;
            if (!b || !b.success) return;
            var total = document.getElementById('pfViewsTotal');
            var d30 = document.getElementById('pfViews30d');
            var byDay = document.getElementById('pfViewsByDay');
            if (total) total.textContent = String(b.total_views || 0);
            if (d30) d30.textContent = String(b.views_last_30d || 0);
            if (byDay && Array.isArray(b.by_day)) {
                b.by_day.slice(-7).forEach(function (d) {
                    byDay.appendChild(el('p', 'pb-stat-line', d.date + ': ' + d.views + ' views'));
                });
            }
            block.hidden = false;
        }).catch(function () { /* stays hidden */ });
    })();

    (function review() {
        var block = document.getElementById('pfReview');
        if (!block) return;
        getJSON('/api/portfolio/review').then(function (res) {
            var b = res.body;
            if (!b || !b.success) return;
            var score = document.getElementById('pfReviewScore');
            var checks = document.getElementById('pfReviewChecks');
            if (score) score.textContent = 'Portfolio score: ' + b.score + '/' + (b.max || 25);
            if (checks && Array.isArray(b.checks)) {
                b.checks.forEach(function (c) {
                    var row = el('p', 'pb-review-check' + (c.passed ? ' passed' : ''));
                    row.appendChild(document.createTextNode((c.passed ? '✓ ' : '○ ') + (c.label || c.id)));
                    if (!c.passed && c.hint) row.appendChild(el('small', null, ' — ' + c.hint));
                    checks.appendChild(row);
                });
            }
            block.hidden = false;
        }).catch(function () { /* stays hidden */ });
    })();

    renderPreview();
})();
