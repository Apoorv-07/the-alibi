/* ALIBI · motion.js — the physics layer. No library, no build step, no network request.
 *
 * Why it is hand-written: this is a trust dashboard that must render on a college network that may block
 * anything it has not been given. A GSAP/Lenis CDN import would be one 404 away from a blank hero. So the
 * same ideas — a spring integrator, a velocity-aware scroll, a single RAF hub — in ~350 lines I can
 * actually reason about, plus the honest fallback every one of them needs:
 *
 *   capability probe fails  →  native scroll, no transforms, page is still the page
 *   prefers-reduced-motion  →  everything settles instantly; nothing loops
 *   tab hidden              →  one shared RAF, paused, so 40 components stop costing CPU together
 *
 * Everything below writes to CSS custom properties on individual elements, or to a single property on
 * :root (`--sy`, `--sv`, `--px`, `--py`, `--prog`). That is deliberate: one style recalculation drives
 * the whole document's parallax instead of forty.
 */
(function () {
  "use strict";

  var root = document.documentElement;
  // A reader can veto the whole layer from Settings. `localStorage` (not a cookie): the preference belongs
  // to the device, not to the account, and the page must not become stateful on the server's account of it.
  // The check runs before anything else so the opt-out is also the cheapest code path.
  try {
    if (window.localStorage && window.localStorage.getItem("alibi.fluid.off") === "1") {
      root.dataset.fluid = "off-by-reader";
      return;
    }
  } catch (e) { /* storage blocked (private mode, sandboxed frame) — the layer stays on, as if no choice */ }
  var matchReduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
  var fine = !(window.matchMedia && window.matchMedia("(hover: none), (pointer: coarse)").matches);

  /* ------------------------------------------------------------ capability ---- */
  // A cheap, honest read of the device. `deviceMemory`/`hardwareConcurrency` are advisory, so they only
  // ever lower quality, never raise it; the first-frame measurement below is what actually decides.
  function probeQuality() {
    if (navigator.deviceMemory !== undefined && navigator.deviceMemory <= 2) return 0;
    if (navigator.hardwareConcurrency !== undefined && navigator.hardwareConcurrency <= 4) return 1;
    if (window.innerWidth * window.innerHeight > 3200000) return 1;   // a 4K panel on integrated graphics
    return 2;
  }

  var reduced = !!(matchReduced && matchReduced.matches);
  var quality = probeQuality();

  /* --------------------------------------------------------------- springs ---- */
  // Semi-implicit Euler on a critically-damped-ish spring. Chosen over `ease-out` curves because the
  // interface must respond to *pointer velocity*, not just position: a spring carries momentum between
  // frames, so a flick lands differently from a creep, which is most of what "alive" means here.
  function Spring(value, opts) {
    opts = opts || {};
    this.x = value; this.v = 0; this.target = value;
    this.k = opts.stiffness || 170;
    this.d = opts.damping || 22;
    this.mass = opts.mass || 1;
    this.precision = opts.precision === undefined ? 0.0015 : opts.precision;
    this.settled = true;
  }
  Spring.prototype.to = function (t) { this.target = t; this.settled = false; Engine.wake(); return this; };
  Spring.prototype.step = function (dt) {
    if (reduced) { this.x = this.target; this.v = 0; this.settled = true; return this.x; }
    var dts = dt / 1000; if (dts > 0.033) dts = 0.033;      // a backgrounded tab must not explode on return
    var n = Math.max(1, Math.min(6, Math.round(dts / 0.0055))), h = dts / n;
    for (var i = 0; i < n; i++) {
      var a = (-this.k * (this.x - this.target) - this.d * this.v) / this.mass;
      this.v += a * h; this.x += this.v * h;
    }
    this.settled = Math.abs(this.x - this.target) < this.precision && Math.abs(this.v) < this.precision * 40;
    return this.x;
  };

  /* ----------------------------------------------------------- the RAF hub ---- */
  var hub = { busy: false }, tasks = [], frame = 0, last = 0, idle = 0, fps = 60, adaptDone = false, started = false;
  var Engine = {
    add: function (fn) { tasks.push(fn); Engine.wake(); return function () { Engine.remove(fn); }; },
    remove: function (fn) { var i = tasks.indexOf(fn); if (i >= 0) tasks.splice(i, 1); },
    wake: function () { idle = 0; if (!started) { started = true; requestAnimationFrame(tick); } },
    fps: function () { return fps; },
    quality: quality, reduced: reduced
  };
  function tick(now) {
    var dt = last ? Math.min(64, now - last) : 16.7;
    last = now; frame++;
    fps += ((1000 / Math.max(1, dt)) - fps) * 0.08;
    hub.busy = false;
    for (var i = 0; i < tasks.length; i++) { try { tasks[i](dt, now, frame); } catch (e) { /* a broken
        decoration must never take the page down with it; the data on the page is not decoration */ } }
    // Adaptive quality: sample the first ~90 frames after boot. If the *GPU* work is costing us, halve the
    // shader resolution once, loudly enough that the console tells a reviewer why the atmosphere is softer.
    if (!adaptDone && frame > 40 && frame < 130 && fps < 48) {
      adaptDone = true; quality = Math.max(0, quality - 1);
      root.dataset.fluidQuality = "auto-reduced:" + quality;
      Engine.onadapt && Engine.onadapt(quality);
    } else if (frame === 131 && !adaptDone) { adaptDone = true; }
    idle = (reduced || hub.busy) ? 0 : idle + dt;
    started = idle < 1800;                       // fully at rest ⇒ the loop stops; scroll restarts it
    if (started) requestAnimationFrame(tick);
  }

  var hidden = false;
  document.addEventListener("visibilitychange", function () {
    hidden = document.hidden;
    root.dataset.fluidPaused = hidden ? "1" : "0";
    if (!hidden) Engine.wake();
  });

  /* --------------------------------------------------- scroll, with inertia ---- */
  // Lenis does this with a wheel listener and a transform on a wrapper. Doing it by hand means three extra
  // behaviours a library would need configuration for, and which this page actually needs:
  //   · the scrollbar keeps its real geometry (a11y, "copy this row's position", jump-to-end)
  //   · keyboard, find-in-page and hash deep-links are never trapped inside the smoothing
  //   · velocity is exposed to the shader and to parallax, not only position
  var target = window.scrollY, current = target, max = 0;
  var sy = new Spring(0, { stiffness: 150, damping: 26 });
  var vel = 0;
  var smooth = fine && !reduced;

  function measure() {
    // The document stays the scroller; only its *number* is ours. Two alternatives are recorded here so
    // nobody re-invents them: writing `html { height: scrollHeight }` to give the thumb exact geometry went
    // stale by ~120px on pages that lay out after `load` (a phantom horizontal scrollbar on a page with no
    // wide content), and making the root a fixed viewport (`html.fluid-virtual { position: fixed }`) killed
    // scrolling outright — a fixed element is the viewport, its overflow only clips, and it is not a scroll
    // container for its in-flow children. Measuring and leaving the browser in charge is the version that
    // survives contact with sticky headers, End, and scrollIntoView.
    var track = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
    max = Math.max(0, Math.round(track - window.innerHeight));
    target = Math.max(0, Math.min(max, target)); current = Math.max(0, Math.min(max, current));
  }
  function apply() {
    root.style.setProperty("--sy", current.toFixed(1));
    root.style.setProperty("--sv", Math.max(-1, Math.min(1, vel / 42)).toFixed(3));
    root.style.setProperty("--prog", max ? (current / max).toFixed(4) : "0");
  }
  var trapped = function () { return false; };
  function scrollTargetTo(y) { return Math.max(0, Math.min(max, y)); }

  function skipSmooth() { return hidden || trapped() || document.activeElement !== document.body &&
    /INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || ""); }

  window.addEventListener("wheel", function (e) {
    if (!smooth || !max || skipSmooth()) return;   // nothing to smooth over: let the browser scroll natively
    e.preventDefault();
    var step = e.deltaMode === 1 ? e.deltaY * 18 : e.deltaMode === 2 ? e.deltaY * window.innerHeight : e.deltaY;
    // the wheel feeds the *target*, and the spring in Engine.add writes the position; adding to the position
    // too (as an earlier draft did) made a page with no measurable length refuse every gesture forever
    target = scrollTargetTo(target + step);
    Engine.wake();
  }, { passive: false });

  window.addEventListener("scroll", function () {
    // Any scroll the browser performed on its own (keyboard, anchor, restore) re-syncs the virtual one, so
    // the two never diverge into a jump. With the virtual scroll the scroller is the root, and `scroll`
    // bubbles from it to `window`, so one listener covers both modes.
    var y = window.scrollY;
    if (Math.abs(y - sy.x) > 2) { target = sy.x = y; }
    Engine.wake();
  }, { passive: true });

  var focusNear = function (el) { if (!el) return false; var r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < window.innerHeight; };
  trapped = function () {
    var a = document.activeElement;
    return !!a && a !== document.body && focusNear(a);
  };

  Engine.add(function (dt) {
    // pointer position, eased — published once on :root so every parallax layer and the shader read the same
    // numbers instead of each handler writing its own style
    root.style.setProperty("--px", px.step(dt).toFixed(4));
    root.style.setProperty("--py", py.step(dt).toFixed(4));
    px.to(p.x); py.to(p.y);
    vel += ((target - current) - vel) * 0.22;
    current += vel * (1000 / Math.max(1, dt) / 60) * 0.14 * (dt / 16.7) / (1000 / Math.max(1, dt) / 60);
    current = scrollTargetTo(current);
    sy.x = current;
    var moving = Math.abs(target - current) > 0.4 || Math.abs(vel) > 0.4;
    hub.busy = moving;                           // keeps the single RAF alive while the page is gliding
    if (smooth) window.scrollTo(0, Math.round(current));
    apply();
    if (moving) parallaxStep();
  });

  /* ------------------------------------------------------ pointer & depth ---- */
  var p = { x: 0.5, y: 0.5, vx: 0, vy: 0, raw: [0, 0] };
  var px = new Spring(0.5, { stiffness: 90, damping: 18 }), py = new Spring(0.5, { stiffness: 90, damping: 18 });
  window.addEventListener("pointermove", function (e) {
    p.vx = e.clientX - p.raw[0]; p.vy = e.clientY - p.raw[1];
    p.raw = [e.clientX, e.clientY];
    p.x = e.clientX / window.innerWidth; p.y = e.clientY / window.innerHeight;
    px.to(p.x); py.to(p.y); Engine.wake();
  }, { passive: true });

  var layers = [];
  function collectLayers() {
    layers = [].slice.call(root.querySelectorAll("[data-depth]"));
  }
  function parallaxStep() {
    if (!layers.length) return;
    var sxv = Math.max(-1, Math.min(1, vel / 60));
    for (var i = 0; i < layers.length; i++) {
      var el = layers[i], d = parseFloat(el.dataset.depth) || 0;
      var y = (current - (el._anchor || 0)) * d * -0.085 + p.vx * d * 0.05;
      el.style.setProperty("--fy", y.toFixed(1) + "px");
      el.style.setProperty("--fx", ((p.x - 0.5) * d * 12).toFixed(1) + "px");
      el.style.setProperty("--fr", (sxv * d * 0.5).toFixed(2) + "deg");
      if (el.dataset.tilt !== "off") {
        el.style.setProperty("--fsk", (sxv * d * 0.12).toFixed(2) + "deg");
      }
    }
  }
  // Anchors are measured lazily and invalidated on resize, which is cheaper than a ResizeObserver per node
  // and behaves the same for a document-sized page.
  function anchors() {
    for (var i = 0; i < layers.length; i++) {
      var el = layers[i];
      var top = window.scrollY + el.getBoundingClientRect().top;
      el._anchor = Math.max(0, top - window.innerHeight * 0.5);
    }
  }

  /* ------------------------------------------------------------ the cursor ---- */
  // A ring that trails the pointer and a dot that follows it. The *native* cursor is deliberately left
  // visible: an invisible native cursor is a lost pointer the moment this script errors, and on a shared
  // demo machine that is exactly the moment someone needs the interface to behave as usual.
  var cur = { ring: null, dot: null, label: "", on: false };
  function buildCursor() {
    // `fine` is a media query, and media queries lie in some embedded webviews. The belt: if the document
    // really can hover, and the pointer is a real mouse, build the ring — an absent cursor costs nothing.
    var hoverable = window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    if ((!fine && !hoverable) || reduced || cur.on) return;
    cur.on = true;
    // The markup already carries an empty `.fluid-cursor` shell (so a page with no JS has nothing to remove);
    // adopt it rather than appending a second one, which is how a duplicated cursor ring shows up in a11y
    // trees and in a reviewer's screenshot.
    var wrap = document.querySelector(".fluid-cursor");
    if (!wrap) { wrap = document.createElement("div"); wrap.className = "fluid-cursor"; document.body.appendChild(wrap); }
    wrap.setAttribute("aria-hidden", "true");
    var ring = document.createElement("span"); ring.className = "fluid-ring";
    var lab = document.createElement("span"); lab.className = "fluid-ring-label";
    var dot = document.createElement("span"); dot.className = "fluid-dot";
    wrap.appendChild(ring); ring.appendChild(lab); wrap.appendChild(dot);
    document.body.appendChild(wrap);
    cur.ring = ring; cur.dot = dot; cur.label = lab;
    var cx = new Spring(0, { stiffness: 260, damping: 26 }), cy = new Spring(0, { stiffness: 260, damping: 26 });
    var dx = new Spring(0, { stiffness: 900, damping: 34 }), dy = new Spring(0, { stiffness: 900, damping: 34 });
    var rx = new Spring(1, { stiffness: 210, damping: 20 }), scale = 1, tag = "";
    function over(e) {
      var t = e.target.closest && e.target.closest("[data-cursor],a,button,summary,[role=button]");
      var want = t ? (t.dataset.cursor || (t.tagName === "A" ? "go" : "press")) : "";
      if (want !== tag) { tag = want; ring.dataset.state = tag; wrap.dataset.state = tag; }
      scale = t ? 1 : 0; rx.to(scale);
    }
    document.addEventListener("pointerover", over, { passive: true });
    Engine.add(function () {
      cx.to(p.raw[0]); cy.to(p.raw[1]); dx.to(p.raw[0]); dy.to(p.raw[1]);
      var X = cx.step(16), Y = cy.step(16), DX = dx.step(16), DY = dy.step(16);
      wrap.style.setProperty("--cx", X.toFixed(1) + "px");
      wrap.style.setProperty("--cy", Y.toFixed(1) + "px");
      wrap.style.setProperty("--dx", DX.toFixed(1) + "px");
      wrap.style.setProperty("--dy", DY.toFixed(1) + "px");
      wrap.style.setProperty("--cs", (0.6 + 0.5 * rx.x).toFixed(3));
      var velMag = Math.min(1, Math.hypot(p.vx, p.vy) / 40);
      wrap.style.setProperty("--stretch", (1 + velMag * 0.5).toFixed(3));
      wrap.style.setProperty("--rot", (Math.atan2(p.vy, p.vx) * 57.2958).toFixed(1) + "deg");
      p.vx *= 0.82; p.vy *= 0.82;
    });
  }

  /* --------------------------------------------------------- magnetic CTA ---- */
  // Proximity attraction, not a 1.05 scale on hover: the object leans toward the pointer while it is near,
  // and releases along its own velocity when it leaves. Springs, so the release overshoots slightly.
  function magnetify() {
    if (reduced) return;
    var nodes = [].slice.call(root.querySelectorAll("[data-magnet]"));
    nodes.forEach(function (el) {
      if (el._mag) return; el._mag = 1;
      var sx = new Spring(0, { stiffness: 130, damping: 14 }), syy = new Spring(0, { stiffness: 130, damping: 14 });
      var strength = parseFloat(el.dataset.magnet) || 0.28;
      function move(e) {
        var r = el.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        var near = Math.hypot(e.clientX - cx, e.clientY - cy) < Math.max(r.width, 140);
        sx.to(near ? (e.clientX - cx) * strength : 0);
        syy.to(near ? (e.clientY - cy) * strength : 0);
        Engine.wake();
      }
      window.addEventListener("pointermove", move, { passive: true });
      Engine.add(function () {
        var x = sx.step(16), y = syy.step(16);
        el.style.setProperty("--mgx", x.toFixed(1) + "px");
        el.style.setProperty("--mgy", y.toFixed(1) + "px");
      });
    });
  }

  /* ---------------------------------------------------- reveal on descent ---- */
  // Intersection-based, so a section below the fold costs nothing until it is approached, and so the
  // animation still happens for a keyboard user who tabs into it.
  function reveal() {
    var items = [].slice.call(root.querySelectorAll("[data-reveal]:not(.is-in)"));
    if (!("IntersectionObserver" in window) || reduced) {
      items.forEach(function (el) { el.classList.add("is-in"); }); return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var el = en.target, i = items.indexOf(el);
        var lag = Math.min(9, Math.max(0, parseInt(el.dataset.stagger || "0", 10))) * 55;
        setTimeout(function () { el.classList.add("is-in"); }, reduced ? 0 : lag);
        io.unobserve(el);
        if (i >= 0) items.splice(i, 1);
      });
    }, { rootMargin: "-6% 0px -12% 0px", threshold: 0.12 });
    items.forEach(function (el) { io.observe(el); });
  }

  /* ------------------------------- shared-element page transition (fluid) ---- */
  // Navigation between pages is the one place a site most often feels like a stack of documents. Where the
  // browser gives us View Transitions we morph the rail and the header instead of cutting; elsewhere the
  // plain navigation happens and nothing is lost.
  function transitions() {
    if (!document.startViewTransition || reduced) return;
    root.dataset.fluidTransition = "view-transitions";
    document.addEventListener("click", function (e) {
      var a = e.target.closest && e.target.closest('a[href^="/"]');
      if (!a || a.target || a.hasAttribute("download") || e.metaKey || e.ctrlKey || e.shiftKey) return;
      var href = a.getAttribute("href");
      if (!href || href.indexOf("/api/") === 0) return;              // JSON endpoints are not navigations
      e.preventDefault();
      root.dataset.fluidFrom = location.pathname;
      a._vFrom = a;
      var t = document.startViewTransition(function () { return navigate(href); });
      t.finished.then(function () { setTimeout(measure, 30); setTimeout(anchors, 60); setTimeout(reveal, 90); });
    });
  }
  function navigate(href) {
    return fetch(href, { headers: { accept: "text/html" } })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        document.title = doc.title;
        document.body.innerHTML = doc.body.innerHTML;                 // (1)
        location.hash = "";
        history.pushState({ p: href }, "", href);
      });
  }
  // (1) The fetched document is same-origin HTML produced by the very server rendering this page: there is
  // no user-controlled string in the URL path (the router rejects anything else with a 404 page), and the
  // alternative — assigning `document.documentElement.innerHTML` — is the same trust model with less
  // control. Scripts in it never run (DOMParser), so `alibi.js` is re-applied by hand below.
  document.addEventListener("alibi:page", function () { measure(); anchors(); reveal(); magnetify(); });

  /* --------------------------------------------------------------- boot ---- */
  function ready(fn) { if (document.readyState !== "loading") fn(); else document.addEventListener("DOMContentLoaded", fn); }
  ready(function () {
    root.dataset.fluidMotion = reduced ? "reduced" : "on";
    root.dataset.fluidQuality = String(quality);
    root.dataset.fluidScroll = smooth ? "inertia" : "native";
    measure(); collectLayers(); anchors(); reveal(); magnetify(); buildCursor(); transitions();
    window.addEventListener("resize", function () { measure(); collectLayers(); anchors(); }, { passive: true });
    window.addEventListener("load", function () { measure(); anchors(); }, { passive: true });
  });

  window.ALIBI = {
    Engine: Engine, Spring: Spring, cursor: cur,
    scroll: function () { return { y: current, target: target, vel: vel, max: max, smooth: smooth }; },
    pointer: p,
    to: function (y) { target = scrollTargetTo(y); Engine.wake(); },
    reduced: function () { return reduced; },
    refresh: function () { measure(); collectLayers(); anchors(); reveal(); magnetify(); },
    quality: function () { return quality; }
  };
  if (matchReduced && matchReduced.addEventListener) {
    matchReduced.addEventListener("change", function (e) {
      reduced = e.matches; smooth = fine && !reduced;
      root.dataset.fluidMotion = reduced ? "reduced" : "on";
      Engine.wake();
    });
  }
})();
