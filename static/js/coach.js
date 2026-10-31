/* Communication coach client (Wave 2 #14, v2). XSS-safe: textContent only. */
(function () {
    "use strict";

    var textarea = document.getElementById("coach-text");
    var btn = document.getElementById("analyze-btn");
    if (!textarea || !btn) return;
    var errBox = document.getElementById("coach-error");
    var results = document.getElementById("results");
    var charCount = document.getElementById("char-count");
    var wordCount = document.getElementById("word-count");
    var estTime = document.getElementById("est-time");
    var qtypeSel = document.getElementById("coach-qtype");
    var answeringLabel = document.getElementById("answering-label");

    var CSRF = (document.querySelector('meta[name="csrf-token"]') || {})
        .content || "";

    var state = { question: null, spokenSeconds: null };

    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }

    function scoreColor(s) {
        return s >= 75 ? "#10b981" : s >= 50 ? "#f59e0b" : "#ef4444";
    }

    function postJSON(url, body) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json",
                       "X-CSRFToken": CSRF },
            body: JSON.stringify(body || {})
        }).then(function (r) {
            return r.json().then(function (d) {
                return { status: r.status, body: d };
            });
        });
    }

    /* ---------- counts ---------- */
    function refreshCounts() {
        var v = textarea.value;
        if (charCount) charCount.textContent = String(v.length);
        var words = v.trim() ? v.trim().split(/\s+/).length : 0;
        if (wordCount) wordCount.textContent = String(words);
        if (estTime) estTime.textContent = String(Math.round(words / 140 * 60));
    }
    textarea.addEventListener("input", refreshCounts);
    refreshCounts();

    /* ---------- question bank ---------- */
    var questions = [];
    try {
        questions = JSON.parse(
            document.getElementById("coach-questions-data").textContent) || [];
    } catch (e) { questions = []; }
    var qGrid = document.getElementById("q-grid");
    var qFramework = document.getElementById("q-framework");

    function showFramework(q) {
        if (!q) { qFramework.className = "ch-framework"; return; }
        document.getElementById("qf-title").textContent = q.question;
        document.getElementById("qf-framework").textContent =
            "Framework: " + (q.framework || "");
        document.getElementById("qf-model").textContent =
            q.model_structure ? "Say it like: " + q.model_structure : "";
        var tips = document.getElementById("qf-tips");
        tips.innerHTML = "";
        (q.tips || []).forEach(function (t) {
            tips.appendChild(el("li", null, t));
        });
        document.getElementById("qf-target").textContent =
            "Target length: about " + (q.target_seconds || 90) +
            " seconds spoken.";
        qFramework.className = "ch-framework is-on";
    }

    function renderQuestions(type) {
        if (!qGrid) return;
        qGrid.innerHTML = "";
        questions.filter(function (q) { return q.type === type; })
            .forEach(function (q) {
                var b = el("button", "ch-q" +
                    (state.question && state.question.id === q.id
                        ? " is-on" : ""), q.question);
                b.type = "button";
                b.addEventListener("click", function () {
                    state.question =
                        (state.question && state.question.id === q.id)
                            ? null : q;
                    if (state.question) {
                        qtypeSel.value = q.type === "behavioral"
                            ? "behavioral"
                            : q.type === "technical" ? "technical" : "hr";
                        answeringLabel.textContent =
                            "Answering: " + q.question;
                    } else {
                        answeringLabel.textContent = "";
                    }
                    showFramework(state.question);
                    renderQuestions(type);
                });
                qGrid.appendChild(b);
            });
    }
    document.querySelectorAll("[data-qtab]").forEach(function (tab) {
        tab.addEventListener("click", function () {
            document.querySelectorAll("[data-qtab]").forEach(function (t) {
                t.className = "ch-tab";
                t.setAttribute("aria-selected", "false");  /* a11y: keep tab state in sync */
            });
            tab.className = "ch-tab is-on";
            tab.setAttribute("aria-selected", "true");
            renderQuestions(tab.getAttribute("data-qtab"));
        });
    });
    renderQuestions("hr");

    /* ---------- speech input ---------- */
    var speakBtn = document.getElementById("speak-btn");
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    var recog = null, speaking = false;
    if (!SR) {
        speakBtn.disabled = true;
        speakBtn.textContent = "\uD83C\uDFA4 Speech not supported in this browser";
    } else {
        speakBtn.addEventListener("click", function () {
            if (speaking) { recog.stop(); return; }
            recog = new SR();
            recog.lang = "en-IN";
            recog.continuous = true;
            recog.interimResults = false;
            recog.onresult = function (ev) {
                var add = "";
                for (var i = ev.resultIndex; i < ev.results.length; i++) {
                    if (ev.results[i].isFinal) {
                        add += ev.results[i][0].transcript + " ";
                    }
                }
                if (add) {
                    textarea.value = (textarea.value + " " + add)
                        .replace(/\s+/g, " ").trimStart();
                    refreshCounts();
                }
            };
            recog.onend = function () {
                speaking = false;
                speakBtn.classList.remove("is-live");
                speakBtn.innerHTML = "\uD83C\uDFA4 Speak your answer";
            };
            recog.onerror = recog.onend;
            recog.start();
            speaking = true;
            speakBtn.classList.add("is-live");
            speakBtn.innerHTML = "\u23F9 Stop speaking";
        });
    }

    /* ---------- timer ---------- */
    var timerBtn = document.getElementById("timer-btn");
    var timerDisplay = document.getElementById("timer-display");
    var timerId = null, timerStart = 0;
    function fmt(sec) {
        var m = Math.floor(sec / 60), s = Math.floor(sec % 60);
        return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
    }
    timerBtn.addEventListener("click", function () {
        if (timerId) {
            clearInterval(timerId);
            timerId = null;
            state.spokenSeconds = Math.round((Date.now() - timerStart) / 1000);
            timerBtn.classList.remove("is-live");
            timerBtn.innerHTML = "\u23F1 Time my answer";
            timerDisplay.textContent = fmt(state.spokenSeconds) +
                " recorded — your pace will be scored";
            return;
        }
        state.spokenSeconds = null;
        timerStart = Date.now();
        timerBtn.classList.add("is-live");
        timerBtn.innerHTML = "\u23F9 Stop timer";
        timerId = setInterval(function () {
            timerDisplay.textContent =
                fmt((Date.now() - timerStart) / 1000);
        }, 250);
    });

    /* ---------- highlighting ---------- */
    function buildHighlighted(text, report) {
        var spans = [];
        (report.filler_hits || []).forEach(function (h) {
            (h.positions || []).forEach(function (pos) {
                spans.push({ start: pos, end: pos + h.word.length,
                             cls: "sev-" + h.severity,
                             title: h.word + " (" + h.category + ", severity " +
                                    h.severity + "/3)" +
                                    (h.alternatives && h.alternatives[0]
                                        ? " — try: " + h.alternatives[0] : "") });
            });
        });
        (report.passive_hits || []).forEach(function (p) {
            spans.push({ start: p.position,
                         end: p.position + (p.match || "").length,
                         cls: "passive", title: "Passive construction — prefer active voice" });
        });
        spans.sort(function (a, b) { return a.start - b.start; });
        var clean = [], lastEnd = -1;
        spans.forEach(function (sp) {
            if (sp.start >= lastEnd) { clean.push(sp); lastEnd = sp.end; }
        });
        var p = el("p", "ch-hl");
        var idx = 0;
        clean.forEach(function (sp) {
            if (sp.start > idx) p.appendChild(document.createTextNode(text.slice(idx, sp.start)));
            var m = document.createElement("mark");
            m.className = sp.cls;
            m.title = sp.title;
            m.textContent = text.slice(sp.start, sp.end);
            p.appendChild(m);
            idx = sp.end;
        });
        if (idx < text.length) p.appendChild(document.createTextNode(text.slice(idx)));
        return p;
    }

    function barRow(label, value, max) {
        var r = el("div", "ch-bar-row");
        r.appendChild(el("span", null, label));
        var bar = el("div", "ch-bar");
        var fill = el("div");
        fill.style.width = Math.max(0, Math.min(100, value / max * 100))
            .toFixed(1) + "%";
        bar.appendChild(fill);
        r.appendChild(bar);
        r.appendChild(el("span", null, value + "/" + max));
        return r;
    }

    /* ---------- report ---------- */
    function renderReport(text, report) {
        results.innerHTML = "";
        var card = el("div", "ch-card");

        if (report.question && report.question.question) {
            var ctx = el("p", null, "Question: " + report.question.question);
            ctx.style.color = "var(--text-muted,#94a3b8)";
            ctx.style.fontSize = "0.9rem";
            card.appendChild(ctx);
        }
        var score = report.clarity_score || 0;
        var ring = el("div", "ch-score-ring");
        ring.style.border = "6px solid " + scoreColor(score);
        ring.textContent = Math.round(score);
        card.appendChild(ring);
        var sub = el("p", null, "Clarity score / 100" +
                     (report.ai_powered ? " · AI-polished rewrite" : ""));
        sub.style.textAlign = "center";
        sub.style.color = "var(--text-muted,#94a3b8)";
        sub.style.fontSize = "0.9rem";
        card.appendChild(sub);

        card.appendChild(el("h3", "ch-h3", "Score breakdown"));
        var bd = report.breakdown || {};
        [["Filler load", "filler_load", 40], ["Pacing", "pacing", 25],
         ["Voice & hedges", "voice_and_hedges", 20],
         ["Structure", "structure", 15]].forEach(function (row) {
            card.appendChild(barRow(row[0], Math.round((bd[row[1]] || 0) * 10) / 10, row[2]));
        });

        var dims = report.dimensions || {};
        if (dims.star) {
            card.appendChild(el("h3", "ch-h3", "Delivery & depth"));
            card.appendChild(barRow("STAR structure", dims.star.score, 100));
            var starRow = el("div", "ch-star");
            [["situation", "Situation"], ["task", "Task"],
             ["action", "Action"], ["result", "Result"]].forEach(function (p) {
                var ok = dims.star.parts && dims.star.parts[p[0]];
                var chip = el("span", ok ? "ok" : "miss",
                              (ok ? "\u2713 " : "\u2717 ") + p[1]);
                starRow.appendChild(chip);
            });
            card.appendChild(starRow);
            if (dims.impact) card.appendChild(barRow("Impact & evidence", dims.impact.score, 100));
            if (dims.confidence) card.appendChild(barRow("Confidence", dims.confidence.score, 100));
            if (dims.delivery) card.appendChild(barRow("Delivery & length", dims.delivery.score, 100));
            if (dims.vocabulary) card.appendChild(barRow("Vocabulary", dims.vocabulary.score, 100));
        }

        var m = report.metrics || {};
        card.appendChild(el("h3", "ch-h3", "Metrics"));
        var grid = el("div", "ch-metrics");
        var metricRows = [["Words", m.word_count], ["Sentences", m.sentence_count],
         ["Avg words/sentence", m.avg_words_per_sentence],
         ["Filler events", m.filler_events],
         ["Passive voice", m.passive_constructions],
         ["Hedges", m.hedge_events]];
        if (dims.delivery) {
            metricRows.push(["Speaking time",
                (dims.delivery.spoken_seconds || dims.delivery.estimated_seconds) + "s"]);
            if (dims.delivery.wpm) metricRows.push(["Your pace", dims.delivery.wpm + " wpm"]);
        }
        if (dims.impact) {
            metricRows.push(["Numbers cited", (dims.impact.metrics_found || []).length]);
            metricRows.push(["Action verbs", dims.impact.action_verbs]);
        }
        metricRows.forEach(function (kv) {
            var box = el("div", "ch-metric");
            box.appendChild(el("div", "v", String(kv[1] === undefined ? "—" : kv[1])));
            box.appendChild(el("div", "l", kv[0]));
            grid.appendChild(box);
        });
        card.appendChild(grid);
        results.appendChild(card);

        var card2 = el("div", "ch-card");
        card2.appendChild(el("h3", "ch-h3", "Your answer — problem spots"));
        card2.appendChild(buildHighlighted(text, report));
        var legend = el("div", "ch-legend");
        [["#ef4444", "Severe filler"], ["#f59e0b", "Moderate filler"],
         ["#3b82f6", "Mild filler"], ["#a855f7", "Passive voice"]].forEach(function (kv) {
            var s = el("span");
            var dot = el("span", "dot");
            dot.style.background = kv[0];
            s.appendChild(dot);
            s.appendChild(document.createTextNode(kv[1]));
            legend.appendChild(s);
        });
        card2.appendChild(legend);
        results.appendChild(card2);

        var sugg = report.suggestions || [];
        if (sugg.length) {
            var card3 = el("div", "ch-card");
            card3.appendChild(el("h3", "ch-h3 fix", "What to improve"));
            var ul = el("ul", "ch-sugg");
            sugg.forEach(function (s) {
                ul.appendChild(el("li", null, s));
            });
            card3.appendChild(ul);
            results.appendChild(card3);
        }

        if (report.cleaned_version) {
            var card4 = el("div", "ch-card");
            card4.appendChild(el("h3", "ch-h3", "Cleaned version"));
            card4.appendChild(el("div", "ch-clean", report.cleaned_version));
            results.appendChild(card4);
        }

        var drills = report.drills || [];
        if (drills.length) {
            var card5 = el("div", "ch-card");
            card5.appendChild(el("h3", "ch-h3", "Practice drills"));
            card5.appendChild(el("p", null, "Say each sentence the clean way — out loud, twice."));
            drills.forEach(function (d) {
                var box = el("div", "ch-drill");
                box.appendChild(el("div", "before", "\u201C" + d.sentence + "\u201D"));
                if (d.after) {
                    box.appendChild(el("div", "after", "Try: \u201C" + d.after + "\u201D"));
                }
                box.appendChild(el("div", "issues",
                    "Issues: " + (d.issues || []).join(", ")));
                card5.appendChild(box);
            });
            results.appendChild(card5);
        }

        var link = el("a", null, "View saved report");
        link.href = report.report_url || "#";
        link.style.color = "var(--accent-primary)";
        link.style.fontWeight = "700";
        results.appendChild(link);

        var reduced = window.matchMedia &&
            window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (!reduced && results.scrollIntoView) {
            results.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    btn.addEventListener("click", function () {
        errBox.textContent = "";
        var text = textarea.value.trim();
        if (text.length < 50) {
            errBox.textContent = "Please paste at least 50 characters of your answer.";
            return;
        }
        if (timerId) timerBtn.click();
        btn.disabled = true;
        btn.textContent = "Analyzing…";
        postJSON("/coach/analyze", {
            text: text,
            question_id: state.question ? state.question.id : null,
            question_type: qtypeSel.value,
            spoken_seconds: state.spokenSeconds
        }).then(function (res) {
            btn.disabled = false;
            btn.textContent = "Analyze My Answer";
            if (res.status !== 200 || !res.body.success) {
                errBox.textContent = (res.body && res.body.error) ||
                    "Analysis failed. Please try again.";
                return;
            }
            res.body.report.report_url = res.body.report_url;
            renderReport(text, res.body.report);
        }).catch(function () {
            btn.disabled = false;
            btn.textContent = "Analyze My Answer";
            errBox.textContent = "Network error. Please try again.";
        });
    });

    /* ---------- video presence ---------- */
    var VIDEO_CHECKS = [
        ["eye_contact", "Look at the camera, not your own video tile", 20],
        ["framing", "Head + shoulders visible, centered, well-lit face", 15],
        ["background", "Clean, uncluttered background (or subtle blur)", 15],
        ["posture", "Sit upright, lean slightly forward — shows engagement", 15],
        ["gestures", "Hands visible occasionally; avoid fidgeting", 10],
        ["audio", "Clear mic, no echo; test before the call", 15],
        ["attire", "Dress one level above the company's daily wear", 10]
    ];
    var checksBox = document.getElementById("video-checks");
    var videoBtn = document.getElementById("video-btn");
    if (checksBox && videoBtn) {
        VIDEO_CHECKS.forEach(function (c) {
            var label = el("label", "ch-check");
            var input = document.createElement("input");
            input.type = "checkbox";
            input.setAttribute("data-video-id", c[0]);
            label.appendChild(input);
            label.appendChild(el("span", null, c[1]));
            label.appendChild(el("span", "pts", c[2] + " pts"));
            checksBox.appendChild(label);
        });
        videoBtn.addEventListener("click", function () {
            var scores = {};
            checksBox.querySelectorAll("input[data-video-id]")
                .forEach(function (input) {
                    scores[input.getAttribute("data-video-id")] =
                        input.checked;
                });
            var box = document.getElementById("video-result");
            postJSON("/coach/video-checklist", { scores: scores })
                .then(function (res) {
                    box.innerHTML = "";
                    if (res.status !== 200 || !res.body.success) {
                        box.appendChild(el("p", "ch-error",
                            (res.body && res.body.error) ||
                            "Could not score the checklist."));
                        return;
                    }
                    var r = res.body.result;
                    var head = el("p", null, "Video readiness: " +
                                  r.score + "/100 — " + r.verdict);
                    head.style.fontWeight = "700";
                    box.appendChild(head);
                    if (r.priority_fixes && r.priority_fixes.length) {
                        box.appendChild(el("p", null,
                            "Top fixes: " + r.priority_fixes.join(" · ")));
                    }
                }).catch(function () {
                    box.innerHTML = "";
                    box.appendChild(el("p", "ch-error",
                        "Network error. Please try again."));
                });
        });
    }
})();
