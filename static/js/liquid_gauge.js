/* Liquid progress gauge (tracker hero).
   Markup: <div class="sb-liquid" data-pct="37"> ...svg... </div>
   The water level, the count-up and the milestone splash are
   driven from here; waves and bubbles are pure CSS. */
(function () {
  "use strict";
  var SVG_NS = "http://www.w3.org/2000/svg";
  // One wave period is 100 units wide; the path spans 400 so the
  // drift loop (translateX -100) is seamless.
  function wavePath(amp, baseY, depth) {
    var d = "M -100 " + baseY;
    for (var x = -100; x < 300; x += 50) {
      d += " q 25 " + (-amp) + " 50 0 t 50 0";
    }
    d += " L 300 " + (baseY + depth) + " L -100 " + (baseY + depth) + " Z";
    return d;
  }
  function build(root) {
    var size = 200, r = 88, cx = 100, cy = 100;
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 200 200");
    svg.setAttribute("role", "img");
    /* a11y (Eval 3, 2026-10-31): role="img" requires an accessible name
       (axe svg-img-alt). Labelled from the same data-* attributes the
       visible percentage text uses. */
    var _lbl = root.getAttribute("data-label") || "Progress";
    var _pct = root.getAttribute("data-pct") || "0";
    svg.setAttribute("aria-label", _lbl + ": " + _pct + "%");
    var defs = document.createElementNS(SVG_NS, "defs");
    var clip = document.createElementNS(SVG_NS, "clipPath");
    clip.setAttribute("id", "lqclip-" + Math.random().toString(36).slice(2, 8));
    var cc = document.createElementNS(SVG_NS, "circle");
    cc.setAttribute("cx", cx); cc.setAttribute("cy", cy); cc.setAttribute("r", r);
    clip.appendChild(cc); defs.appendChild(clip);
    var grad = document.createElementNS(SVG_NS, "linearGradient");
    grad.setAttribute("id", "lqgrad-" + clip.id.slice(7));
    grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0");
    grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
    [["0%", "#3d6bff"], ["100%", "#1b34c9"]].forEach(function (s) {
      var st = document.createElementNS(SVG_NS, "stop");
      st.setAttribute("offset", s[0]); st.setAttribute("stop-color", s[1]);
      grad.appendChild(st);
    });
    defs.appendChild(grad); svg.appendChild(defs);
    var bg = document.createElementNS(SVG_NS, "circle");
    bg.setAttribute("cx", cx); bg.setAttribute("cy", cy); bg.setAttribute("r", r);
    bg.setAttribute("fill", "rgba(255,255,255,0.04)");
    svg.appendChild(bg);
    // The clip must sit on a STATIC wrapper: if it were on the
    // moving group, the clip window would slide with the water.
    var clipWrap = document.createElementNS(SVG_NS, "g");
    clipWrap.setAttribute("clip-path", "url(#" + clip.id + ")");
    var water = document.createElementNS(SVG_NS, "g");
    water.setAttribute("class", "lq-water");
    var body = document.createElementNS(SVG_NS, "path");
    body.setAttribute("d", wavePath(7, 0, 212));
    body.setAttribute("fill", "url(#" + grad.id + ")");
    water.appendChild(body);
    var waveB = document.createElementNS(SVG_NS, "path");
    waveB.setAttribute("d", wavePath(5, 2, 15));
    waveB.setAttribute("fill", "rgba(140,175,255,0.5)");
    waveB.setAttribute("class", "lq-wave-b");
    water.appendChild(waveB);
    var waveA = document.createElementNS(SVG_NS, "path");
    waveA.setAttribute("d", wavePath(6, 5, 10));
    waveA.setAttribute("fill", "rgba(200,255,46,0.28)");
    waveA.setAttribute("class", "lq-wave-a");
    water.appendChild(waveA);
    [[38, 3.2, ""], [78, 2.4, "b2"], [122, 3.8, "b3"], [158, 2.1, "b4"], [98, 1.8, "b2"]].forEach(function (b) {
      var bub = document.createElementNS(SVG_NS, "circle");
      bub.setAttribute("cx", b[0]); bub.setAttribute("cy", 196);
      bub.setAttribute("r", b[1]);
      bub.setAttribute("fill", "rgba(255,255,255,0.5)");
      bub.setAttribute("class", "lq-bubble " + b[2]);
      water.appendChild(bub);
    });
    clipWrap.appendChild(water);
    svg.appendChild(clipWrap);
    var pulse = document.createElementNS(SVG_NS, "circle");
    pulse.setAttribute("cx", cx); pulse.setAttribute("cy", cy); pulse.setAttribute("r", r + 4);
    pulse.setAttribute("fill", "none");
    pulse.setAttribute("stroke", "rgba(200,255,46,0.8)");
    pulse.setAttribute("stroke-width", "2");
    pulse.setAttribute("class", "lq-pulse");
    svg.appendChild(pulse);
    var ring = document.createElementNS(SVG_NS, "circle");
    ring.setAttribute("cx", cx); ring.setAttribute("cy", cy); ring.setAttribute("r", r + 4);
    ring.setAttribute("fill", "none");
    ring.setAttribute("stroke", "rgba(242,241,234,0.35)");
    ring.setAttribute("stroke-width", "1.5");
    svg.appendChild(ring);
    water.style.transform = "translateY(196px)";
    var num = document.createElement("div");
    num.className = "sb-liquid-num";
    num.innerHTML = "<b>0%</b><span></span>";
    root.appendChild(svg);
    root.appendChild(num);
    return { water: water, numVal: num.querySelector("b"), numLabel: num.querySelector("span"), pct: 0, svg: svg };
  }
  function setPct(inst, root, target, label) {
    target = Math.max(0, Math.min(100, target || 0));
    if (label !== undefined) inst.numLabel.textContent = label;
    /* a11y (Eval 3): keep the svg's accessible name in sync with the value. */
    var _ariaName = (label !== undefined && label) ? label : (inst.numLabel.textContent || "Progress");
    inst.svg.setAttribute("aria-label", _ariaName + ": " + Math.round(target) + "%");
    // Surface travels from y=196 (empty, hidden below the disc)
    // to y=6 (full). The CSS transition on .lq-water animates it.
    var y = 196 - (target / 100) * 190;
    inst.water.style.transform = "translateY(" + y.toFixed(1) + "px)";
    var from = inst.pct, start = null, dur = 1300;
    var milestones = [25, 50, 75, 100];
    function frame(ts) {
      if (!start) start = ts;
      var t = Math.min(1, (ts - start) / dur), e = 1 - Math.pow(1 - t, 3);
      var val = from + (target - from) * e;
      inst.numVal.textContent = Math.round(val) + "%";
      milestones.forEach(function (m) {
        if (from < m && val >= m) {
          root.classList.remove("is-splash"); void root.offsetWidth;
          root.classList.add("is-splash");
        }
      });
      if (t < 1) requestAnimationFrame(frame); else inst.pct = target;
    }
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      inst.numVal.textContent = Math.round(target) + "%"; inst.pct = target;
    } else {
      requestAnimationFrame(frame);
    }
  }
  window.SBLiquid = {
    mount: function (root) {
      var inst = build(root);
      root._lq = inst;
      var pct = parseFloat(root.getAttribute("data-pct") || "0");
      var label = root.getAttribute("data-label") || "";
      // Start empty, then fill — the rise IS the entrance.
      requestAnimationFrame(function () { setTimeout(function () { setPct(inst, root, pct, label); }, 150); });
      var api = { set: function (p, l) { setPct(inst, root, p, l); } };
      root._lqApi = api;  // live updates (e.g. tracker refresh) reuse this
      return api;
    }
  };
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".sb-liquid[data-pct]").forEach(function (el) {
      if (!el._lq) window.SBLiquid.mount(el);
    });
  });
})();
