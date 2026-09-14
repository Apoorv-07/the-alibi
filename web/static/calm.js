/* calm.js — the only script the six human pages load, and every one of its behaviours has a
 * no-JavaScript equivalent already in the HTML. It exists because three interactions are genuinely worse
 * as a page reload, not because a page needs JS to be understood:
 *
 *   1. an Ask suggestion should fill the question box, not navigate away from what you were reading;
 *   2. a "why" disclosure deep-linked from another page should open itself, so the link lands on an answer;
 *   3. a chosen review option should submit, because "Use that" next to a radio is two clicks for one act.
 *
 * No scroll listeners, no animation loops, no layout writes on the frame clock — the calm pages have
 * nothing to animate. If this file throws, the pages are unaffected: everything runs from a delegate on
 * `document` and each handler is wrapped.
 */
(function () {
  "use strict";

  function on(sel, type, fn) {
    document.addEventListener(type, function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var hit = t.closest(sel);
      if (hit) { try { fn(hit, ev); } catch (e) { /* a broken enhancement must stay an enhancement */ } }
    }, { passive: true });
  }

  /* 1 · Ask chips -------------------------------------------------------- */
  on("[data-ask]", "click", function (btn) {
    var input = document.getElementById("ask-q");
    if (!input) return;
    input.value = btn.getAttribute("data-ask");
    input.focus();
    input.form && input.form.requestSubmit ? input.form.requestSubmit() : input.form && input.form.submit();
  });

  /* 2 · deep links into a disclosure ------------------------------------- */
  function openTargeted() {
    var id = location.hash.slice(1);
    if (!id) return;
    var host = document.getElementById(id);
    if (!host) return;
    var d = host.matches("details") ? host : host.querySelector("details");
    if (d) d.open = true;
    if (host.scrollIntoView) host.scrollIntoView({ block: "center", behavior: "auto" });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", openTargeted);
  } else { openTargeted(); }

  /* 3 · a note typed for one verb is a note for both -----------------------
     "I'll decide later" lives in its own <form> (a form cannot nest), so its hidden `note` field is a second
     input with the same name. Without this, the sentence "the portal updates at midnight, so the notice is
     stale" would be saved when you decide and dropped exactly when you postpone — and postponing is when a
     reason matters most. */
  on(".rev input[id^=note-]", "input", function (src) {
    var mirror = document.querySelector('[data-mirror="' + src.id + '"]');
    if (mirror) mirror.value = src.value;
  });

  /* 4 · a chosen option is the decision ---------------------------------- */
  on(".opt input[type=radio]", "change", function (radio) {
    var form = radio.form;
    if (!form || !form.querySelector(".rev__actions")) return;   // the technical queue keeps its own button
    var submit = form.querySelector('button[type=submit]');
    if (submit) submit.textContent = "Using that…";
  });

  /* 5 · one write on any of these pages should say what it did ----------------------------
     The technical pages get this from `alibi.js`, which the calm pages deliberately do not load. So the
     same promise is kept in a smaller file: submit by fetch, report in one sentence, then re-read the page
     — because a calm page whose numbers silently stay stale after you answered something is not calm, it is
     wrong. A form marked `data-silent` (a file upload, whose answer is a whole new ingest report) keeps the
     browser's own behaviour and its own results panel. If `fetch` fails or the response is not JSON, the
     form submits normally: no page should be *unable to save* because a nicety broke. */
  function say(kind, title, body) {
    var layer = document.querySelector(".toast-layer");
    if (!layer) {
      layer = document.createElement("div");
      layer.className = "toast-layer";
      layer.setAttribute("role", "status");
      layer.setAttribute("aria-live", "polite");
      layer.setAttribute("aria-label", "notifications");
      document.body.appendChild(layer);
    }
    var el = document.createElement("div");
    el.className = "toast toast--" + kind;
    var h = document.createElement("b"); h.textContent = title; el.appendChild(h);
    if (body) { var s = document.createElement("span"); s.textContent = body; el.appendChild(s); }
    var b = document.createElement("button");
    b.className = "toast__x"; b.textContent = "dismiss";
    b.setAttribute("aria-label", "dismiss this message");
    b.addEventListener("click", function () { el.remove(); });
    el.appendChild(b);
    layer.appendChild(el);
    setTimeout(function () { if (el.parentNode) el.remove(); }, 6500);
  }

  function summarize(j) {
    if (!j || typeof j !== "object") return null;
    if (j.ok === false) return { k: "bad", t: "not accepted", b: String(j.error || j.detail || "").slice(0, 240) };
    if (j.decision) return { k: j.decision === "APPROVAL_REQUIRED" ? "info" : "ok", t: j.decision, b: j.reason || "" };
    if (j.recorded) return { k: "ok", t: "recorded", b: "claim " + j.claim_id + " · " + (j.shape || "") };
    if (j.claims !== undefined || j.sources !== undefined) {
      var bits = [];
      if (j.claims) bits.push(j.claims + " claim(s)");
      if (j.conflicts) bits.push(j.conflicts + " conflict(s)");
      if (j.reviews) bits.push(j.reviews + " question(s) for you");
      if (j.invalid_outputs) bits.push(j.invalid_outputs + " refused for not matching their source");
      return { k: "ok", t: "sources re-read", b: bits.join(" · ") || "nothing new to read" };
    }
    if (j.ambiguous) return { k: "warn", t: "that named two dates", b: j.next || "pick one in Review" };
    return { k: "ok", t: "done", b: "" };
  }

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form || !form.action || form.dataset.silent === "on" || form.method !== "post") return;
    if (!/^\/api\//.test(String(form.getAttribute("action") || ""))) return;
    if (!window.fetch) return;                                  // let the browser do it
    ev.preventDefault();
    var btn = form.querySelector("button[type=submit],button:not([type])");
    if (btn) { btn.disabled = true; btn.dataset.was = btn.textContent; btn.textContent = "Working…"; }
    fetch(form.action, { method: "POST", body: new FormData(form), headers: { "Accept": "application/json" } })
      .then(function (res) {
        return res.text().then(function (txt) {
          var j = null;
          try { j = JSON.parse(txt); } catch (e) { j = { ok: res.ok, detail: txt.slice(0, 240) }; }
          var s = summarize(j) || { k: res.ok ? "ok" : "bad", t: res.ok ? "done" : "failed", b: "" };
          if (!res.ok && res.status !== 422) s = { k: "bad", t: "not accepted (" + res.status + ")", b: s.b };
          say(s.k, s.t, s.b);
          if (s.k !== "bad") setTimeout(function () { location.reload(); }, 700);
          else if (btn) { btn.disabled = false; btn.textContent = btn.dataset.was || "Try again"; }
        });
      })
      .catch(function (e) {
        say("bad", "request failed", String((e && e.message) || e));
        if (btn) { btn.disabled = false; btn.textContent = btn.dataset.was || "Retry"; }
      });
  }, { passive: false });

  /* The enhancement is announced to the page so a stylesheet can rely on it (e.g. `:has` fallbacks), and
     so a test can tell "the script ran" from "the script exists". */
  document.documentElement.dataset.calmEnhanced = "1";
})();
