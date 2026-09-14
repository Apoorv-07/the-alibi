/* ALIBI — layer 2 of the interaction model. ~120 lines, one file, no build, no dependencies.
 *
 * The rule for everything in this file: the page must be *correct* without it. Every action here is also
 * a plain form or link that the server handles on its own; this script only (a) tells you what a POST did
 * instead of leaving you with a JSON blob, (b) lets you reach any page in two keystrokes, and (c) marks a
 * busy button busy. Nothing here decides whether a claim is trusted — that is `alibi/ground.py`'s job and
 * always has been.
 */
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------------------------------------------------------- toasts ---- */
  var layer = null;
  function toast(kind, title, body, ms) {
    if (!layer) {
      layer = document.createElement("div");
      layer.className = "toast-layer";
      layer.setAttribute("role", "status");
      layer.setAttribute("aria-live", "polite");
      layer.setAttribute("aria-label", "notifications");
      document.body.appendChild(layer);
    }
    var el = document.createElement("div");
    el.className = "toast " + (kind || "ok");
    var h = document.createElement("b");
    h.textContent = title;
    el.appendChild(h);
    if (body) {
      var p = document.createElement("span");
      p.textContent = body;
      el.appendChild(p);
    }
    var close = document.createElement("button");
    close.textContent = "dismiss";
    close.className = "toast-dismiss mini";
    close.setAttribute("aria-label", "dismiss this message");
    close.addEventListener("click", function () { kill(el); });
    el.appendChild(close);
    layer.appendChild(el);
    if (!reduced) setTimeout(function () { kill(el); }, ms || 6500);
    return el;
  }
  function kill(el) {
    if (!el || !el.parentNode) return;
    el.classList.add("out");
    setTimeout(function () { el.remove(); }, reduced ? 0 : 200);   // with motion off, it simply leaves
  }

  /* An error from this app is never a bare status code: the API returns `detail` sentences that name the
     rule that fired and what to do next. Surface them instead of losing them in a devtools tab. */
  function report(id, res, text) {
    var j = null;
    try { j = JSON.parse(text); } catch (e) { /* an HTML redirect, or an empty body */ }
    if (!res.ok) {
      toast("bad", "not accepted (" + res.status + ")", (j && j.detail) || text.slice(0, 240));
      return false;
    }
    if (j && j.ambiguous) {
      toast("warn", "your answer named " + (j.candidates || []).length + " dates",
            (j.candidates || []).join(" · ") + " — " + (j.next || ""));
      return true;
    }
    if (j && j.decision) toast(j.decision === "APPROVAL_REQUIRED" ? "info" : "ok", j.decision, j.reason || "");
    else if (j && j.claims !== undefined) toast("ok", "sync complete",
      j.claims + " claim(s) added from " + j.sources + " source(s); " + j.rejected + " block(s) quarantined");
    else if (j && j.recorded) toast("ok", "recorded as claim " + j.claim_id,
      "method=manual — your answer, with your sentence as the receipt");
    else if (id === "wipe") toast("bad", "ledger erased", JSON.stringify((j && j.remaining_rows) || {}));
    else toast("ok", "done", "");
    return true;
  }

  /* every form that posts to the API: submit by fetch, then toast + reload on success */
  document.addEventListener("submit", function (ev) {
    var f = ev.target;
    if (!f || !f.action || f.action.indexOf("/api/") < 0 && f.action.indexOf("/me/wipe") < 0) return;
    if (f.method.toUpperCase() !== "POST") return;
    var btns = [].slice.call(f.querySelectorAll("button,input[type=submit]"));
    var hit = ev.submitter || btns[0];
    if (hit && hit.name === "decision" && !hit.value) return;      // a decision button must be chosen
    if (f.dataset.busy) return;
    f.dataset.busy = "1";
    btns.forEach(function (b) { b.disabled = true; });
    if (hit) { hit.dataset.was = hit.textContent; hit.textContent = "working…"; }
    var isWipe = f.action.indexOf("/me/wipe") >= 0;
    if (isWipe && !window.confirm("This DELETEs every claim, source and receipt. The response will show the row counts the database reports afterwards. Continue?")) {
      f.removeAttribute("data-busy");
      btns.forEach(function (b) { b.disabled = false; });
      if (hit) hit.textContent = hit.dataset.was;
      ev.preventDefault();
      return;
    }
    ev.preventDefault();
    fetch(f.action, { method: "POST", body: new FormData(f), headers: { "accept": "application/json" } })
      .then(function (res) {
        return res.text().then(function (text) {
          var ok = report(isWipe ? "wipe" : "", res, text);
          if (ok && res.ok) setTimeout(function () { location.reload(); }, 900);
          else {
            f.removeAttribute("data-busy");
            btns.forEach(function (b) { b.disabled = false; if (b === hit) b.textContent = b.dataset.was; });
          }
        });
      })
      .catch(function (e) {
        toast("bad", "request failed", String(e && e.message || e));
        f.removeAttribute("data-busy");
        btns.forEach(function (b) { b.disabled = false; if (b === hit) b.textContent = b.dataset.was; });
      });
  }, true);

  /* ------------------------------------------------ command palette ---- */
  function navEntries() {
    return [].slice.call(document.querySelectorAll("nav a[href]")).map(function (a) {
      return { label: a.textContent.replace(/\s+/g, " ").trim(), href: a.getAttribute("href") };
    });
  }
  function palette() {
    var old = document.getElementById("palette");
    if (old) { old.remove(); return; }
    var wrap = document.createElement("div");
    wrap.className = "palette"; wrap.id = "palette";
    wrap.setAttribute("role", "dialog"); wrap.setAttribute("aria-modal", "true");
    wrap.setAttribute("aria-label", "go to a page");
    var box = document.createElement("div"); box.className = "palette-box";
    var input = document.createElement("input");
    input.placeholder = "go to… (timeline, conflicts, queue, metrics…)";
    input.setAttribute("aria-label", "filter pages");
    var list = document.createElement("ul"); list.className = "palette-list";
    box.appendChild(input); box.appendChild(list); wrap.appendChild(box);
    document.body.appendChild(wrap);
    var all = navEntries().concat([
      { label: "export ledger.json", href: "/api/export/ledger.json" },
      { label: "export ledger.csv", href: "/api/export/ledger.csv" },
      { label: "export twin.ics", href: "/api/export/twin.ics" },
      { label: "sync the demo corpus", href: "#sync" },
      { label: "AI runtime check", href: "/api/ai-check" }
    ]);
    var cur = 0;
    function draw() {
      var q = input.value.toLowerCase();
      var hits = all.filter(function (e) { return !q || e.label.toLowerCase().indexOf(q) >= 0; }).slice(0, 12);
      while (list.firstChild) list.removeChild(list.firstChild);   // never innerHTML: this file's only
      if (!hits.length) {                                          // job is navigation, and a DOM API
        var li = document.createElement("li");                     // that accepts markup is how a
        var hint = document.createElement("span");                // query string becomes an XSS surface
        hint.setAttribute("style", "padding:7px 9px;display:block;color:#7d8ea3");
        hint.textContent = "no page matches that. Try: timeline · conflicts · queue · lineage · eval · metrics";
        li.appendChild(hint);
        list.appendChild(li);
        return;
      }
      hits.forEach(function (e, i) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = e.href; a.textContent = e.label;
        if (i === cur) a.setAttribute("aria-current", "true");
        var s = document.createElement("span"); s.textContent = e.href;
        a.appendChild(s);
        li.appendChild(a);
        if (e.href === "#sync") {
          a.addEventListener("click", function (ev2) {
            ev2.preventDefault();
            var form = document.querySelector('form[action="/api/sync"]');
            if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
          });
        }
        list.appendChild(li);
      });
    }
    function move(d) {
      var n = list.querySelectorAll("a").length;
      if (!n) return;
      cur = (cur + d + n) % n; draw();
    }
    input.addEventListener("input", function () { cur = 0; draw(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") {
        var a = list.querySelectorAll("a")[cur];
        if (a) { e.preventDefault(); a.click(); }
      } else if (e.key === "Escape") { wrap.remove(); }
    });
    wrap.addEventListener("click", function (e) { if (e.target === wrap) wrap.remove(); });
    draw();
    input.focus();
  }

  var pending = null;
  document.addEventListener("keydown", function (e) {
    var typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); palette(); return; }
    if (e.key === "Escape") { var p = document.getElementById("palette"); if (p) p.remove(); return; }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "?") {
      toast("info", "keys", "⌘/Ctrl-K or g then a letter: jump · Enter: submit a focused form · " +
            "every number links to its receipt", 9000);
      return;
    }
    if (e.key === "g") { pending = Date.now(); return; }
    if (pending && Date.now() - pending < 1200) {
      var want = { t: "timeline", c: "conflicts", q: "queue", l: "lineage", f: "feasibility",
                   s: "sources", r: "risk", e: "eval", a: "actions", m: "metrics", n: "claims" }[e.key.toLowerCase()];
      pending = null;
      if (want) {
        var link = navEntries().filter(function (x) { return x.label.toLowerCase().indexOf(want.slice(0, 4)) >= 0; })[0];
        if (link) location.href = link.href;
      }
    }
  });

  /* ------------------------------------------- small quality of life ---- */
  // A long quote block is the one place where "click to copy the claim id" earns its keep.
  document.addEventListener("click", function (e) {
    var code = e.target.closest && e.target.closest("code[data-copy]");
    if (!code) return;
    (navigator.clipboard ? navigator.clipboard.writeText(code.textContent) : Promise.reject())
      .then(function () { toast("ok", "copied", code.textContent, 2200); })
      .catch(function () { toast("warn", "clipboard blocked", code.textContent, 4000); });
  });
  // Show a filter input's effect live; the server-side GET form still does the real work.
  var filter = document.querySelector("[data-live-filter]");
  if (filter) {
    var scope = document.querySelector(filter.getAttribute("data-live-filter")) || document;
    filter.addEventListener("input", function () {
      var q = filter.value.toLowerCase();
      [].slice.call(scope.querySelectorAll("tbody tr")).forEach(function (tr) {
        tr.hidden = !!q && tr.textContent.toLowerCase().indexOf(q) < 0;
      });
    });
  }
  // Deep links to #c12 should not land with the row off-screen.
  if (location.hash.length > 2) {
    var anchor = document.getElementById(location.hash.slice(1));
    if (anchor && anchor.scrollIntoView) {
      anchor.style.transition = "background .6s";
      anchor.style.background = "#16212e";
      anchor.scrollIntoView({ block: "center" });
      setTimeout(function () { anchor.style.background = ""; }, 1400);
    }
  }

  /* ------------------------------------------- presence (§ of the brief) ---- */
  /* Three categories of motion, deliberately separated so the page never becomes exhausting:
       ambient    — the shader and the orbs; slow, paused when the tab is hidden
       interactive — springs, magnetism, the cursor; they only move when you move
       narrative  — reveals and the section signal below; they fire once, on arrival
     Nothing in this block animates on a timer. */
  function presence() {
    var A = window.ALIBI;
    if (!A || A.reduced()) return;
    var rail = document.querySelector(".rail") || document.querySelector("nav");
    var links = rail ? [].slice.call(rail.querySelectorAll("a[href^='/']")) : [];
    var heads = [].slice.call(document.querySelectorAll("[data-section], h2, .strip, .scene-head"));
    if (!links.length || !heads.length) return;
    heads.forEach(function (h, i) { if (!h.id) h.id = "s-" + i; });
    var seen = {};
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        seen[en.target.id] = en.isIntersecting ? en.intersectionRatio : 0;
      });
      var best = null, bestScore = 0;
      heads.forEach(function (h) {
        var s = seen[h.id] || 0;
        if (s > bestScore) { bestScore = s; best = h; }
      });
      if (!best) return;
      // A nav href is a *path*, not a selector: `best.closest("/")` is a SyntaxError, and it cost this page a
      // console error on every scroll. Resolve the path to the element it points at, then compare ancestors.
      var link = null;
      links.forEach(function (a) {
        var href = a.getAttribute("href").split("?")[0];
        var target = href === "/" ? document.getElementById("page-") : null;
        if (!target) target = document.getElementById("page-" + href.replace(/^\/+|\/+$/g, ""));
        if (target && (target === best || target.contains(best))) link = a;
      });
      if (link) rail.querySelectorAll("a").forEach(function (a) { a.dataset.near = a === link ? "1" : "0"; });
    }, { threshold: [0, .15, .4, .8] });
    heads.forEach(function (h) { io.observe(h); });
  }

  /* Objects with `data-tilt` lean toward the pointer in 3D. Applied only where the object is a thing you
     can open (a conflict, a review item) — a page where every surface tilts is a page with no hierarchy. */
  function tilt() {
    if (window.ALIBI && window.ALIBI.reduced()) return;
    if (!(window.matchMedia && window.matchMedia("(hover: hover)").matches)) return;
    [].slice.call(document.querySelectorAll(".obj[data-cursor], .figure--lead")).forEach(function (el) {
      if (el._tilt) return; el._tilt = 1;
      el.style.setProperty("--tilt-persp", "900px");
      el.addEventListener("pointermove", function (e) {
        var r = el.getBoundingClientRect();
        el.style.setProperty("--tiltx", (((e.clientY - r.top) / r.height - .5) * -4).toFixed(2) + "deg");
        el.style.setProperty("--tilty", (((e.clientX - r.left) / r.width - .5) * 5).toFixed(2) + "deg");
      });
      el.addEventListener("pointerleave", function () {
        el.style.setProperty("--tiltx", "0deg"); el.style.setProperty("--tilty", "0deg");
      });
    });
  }

  /* On a fluid navigation the enhancement layer has to be re-applied by hand: `motion.js` swapped the body,
     which by design does not re-run scripts. Same for the first paint. */
  function enhance() { presence(); tilt(); if (window.ALIBI && window.ALIBI.refresh) window.ALIBI.refresh(); }
  if (document.readyState !== "loading") enhance(); else document.addEventListener("DOMContentLoaded", enhance);
  document.addEventListener("alibi:page", enhance);
})();
