/* ALIBI · scene.js — the field of facts. WebGL2 instanced billboards, one draw call for the nodes, one
 * for the edges. No Three.js: this scene needs points, rings and lines, not a mesh loader, and a trust
 * dashboard must not depend on a CDN for its one visual flourish.
 *
 * What the field actually is: every node is one live row of the ledger — a subject, a predicate, a value,
 * the state the verifier gave it. Nothing is invented for looks.
 *
 *      angle around the ring   = the subject (one cluster per course/task group, so a cluster IS a course)
 *      height                   = how soon the obligation bites (soonest rises)
 *      radius                   = authority tier of the source that grounded it
 *      colour                   = grounded / in review / in conflict — the same three states the tables use
 *      size                     = 1 row, not a gradient: a claim is either there or not
 *      line between two nodes  = a recorded supersession or dependency edge
 *
 * The canvas is `aria-hidden` and the identical information ships as a focusable list right next to it, so
 * a keyboard or screen-reader user gets the same index without the geometry. The canvas is a *link field*:
 * click a node and you land on its claim row (`/claims#c12`), which is the same destination as the list.
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

  var NODE_VS = `#version 300 es
precision highp float;
layout(location=0) in vec4 aData;    // xyz = world position, w = size
layout(location=1) in vec2 aCorner;  // the two-triangle unit quad for this instance
layout(location=2) in vec2 aStyle;   // x = hue 0..1, y = mode (0 disc, 1 ring, 2 diamond)
uniform mat4 uProj, uView, uModel;
uniform float uPx;
out vec2 vC; out float vHue; out float vMode; out float vFade;
void main() {
  vec4 world = uModel * vec4(aData.xyz, 1.0);
  vec4 view = uView * world;
  vec4 clip = uProj * view;
  float d = max(0.4, -view.z);
  float rad = aData.w * uPx / d;
  gl_Position = clip + vec4(aCorner * rad * clip.w, 0.0, 0.0);
  vC = aCorner; vHue = aStyle.x; vMode = aStyle.y;
  vFade = clamp(1.35 - d * 0.18, 0.14, 1.0);
}`;

  var NODE_FS = `#version 300 es
precision highp float;
in vec2 vC; in float vHue; in float vMode; in float vFade;
out vec4 outColor;
uniform vec3 uGround, uReview, uConflict;
void main() {
  vec2 p = vC * 2.0;
  float r = length(p);
  float shape;
  if (vMode < 0.5) shape = smoothstep(1.0, 0.35, r);                       // a grounded fact: solid
  else if (vMode < 1.5) shape = smoothstep(1.0, 0.78, r) * smoothstep(0.42, 0.62, r);  // review: open ring
  else shape = smoothstep(1.0, 0.2, abs(p.x) + abs(p.y));                  // conflict: diamond
  float halo = exp(-r * r * 1.6) * 0.34;
  vec3 c = vHue < 0.34 ? uGround : (vHue < 0.67 ? uReview : uConflict);
  float a = clamp(shape + halo, 0.0, 1.0) * vFade;
  if (a < 0.003) discard;
  outColor = vec4(c * (0.55 + 0.85 * shape), a);
}`;

  var LINE_VS = `#version 300 es
precision highp float;
layout(location=0) in vec4 aPos;     // xyz world, w = alpha
uniform mat4 uProj, uView, uModel;
out float vA;
void main() {
  gl_Position = uProj * uView * uModel * vec4(aPos.xyz, 1.0);
  vA = aPos.w;
}`;
  var LINE_FS = `#version 300 es
precision highp float;
in float vA; out vec4 outColor;
uniform vec3 uTint;
void main() { outColor = vec4(uTint, vA * 0.30); }`;

  function mat4() { return new Float32Array(16); }
  function perspective(o, fovY, aspect, near, far) {
    var f = 1 / Math.tan(fovY / 2), nf = 1 / (near - far);
    o[0] = f / aspect; o[1] = 0; o[2] = 0; o[3] = 0;
    o[4] = 0; o[5] = f; o[6] = 0; o[7] = 0;
    o[8] = 0; o[9] = 0; o[10] = (far + near) * nf; o[11] = -1;
    o[12] = 0; o[13] = 0; o[14] = 2 * far * near * nf; o[15] = 0;
    return o;
  }
  function viewMatrix(o, tx, ty, tz) {
    o[0] = 1; o[1] = 0; o[2] = 0; o[3] = 0;
    o[4] = 0; o[5] = 1; o[6] = 0; o[7] = 0;
    o[8] = 0; o[9] = 0; o[10] = 1; o[11] = 0;
    o[12] = -tx; o[13] = -ty; o[14] = -tz; o[15] = 1;
    return o;
  }
  function modelMatrix(o, ry, rx) {
    var cy = Math.cos(ry), sy = Math.sin(ry), cx = Math.cos(rx), sx = Math.sin(rx);
    o[0] = cy; o[1] = 0; o[2] = -sy; o[3] = 0;
    o[4] = sy * sx; o[5] = cx; o[6] = cy * sx; o[7] = 0;
    o[8] = sy * cx; o[9] = -sx; o[10] = cy * cx; o[11] = 0;
    o[12] = 0; o[13] = 0; o[14] = 0; o[15] = 1;
    return o;
  }
  function project(mvp, x, y, z, w, h) {
    var cx = mvp[0] * x + mvp[4] * y + mvp[8] * z + mvp[12];
    var cy = mvp[1] * x + mvp[5] * y + mvp[9] * z + mvp[13];
    var cw = mvp[3] * x + mvp[7] * y + mvp[11] * z + mvp[15] || 1;
    return [(cx / cw * 0.5 + 0.5) * w, (1 - (cy / cw * 0.5 + 0.5)) * h, cw];
  }

  function boot() {
    // `.scene`, not `#scene`: the field is emitted as a `<figure class="scene">` by `_scene()` in
    // `alibi/server.py`, and this line silently returned on every page until a browser probe compared the
    // canvas size with its CSS box (300×150 forever). A mount must be asserted by measurement, not by hope.
    var host = document.querySelector(".scene");
    var dataEl = document.getElementById("scene-data");
    if (!host || !dataEl) { if (host) host.dataset.fluid = "unmounted"; return; }
    var canvas = host.querySelector("canvas");
    var data;
    try { data = JSON.parse(dataEl.textContent); } catch (e) { canvas.dataset.fluid = "bad-data"; return; }
    var claims = data.claims || [], edges = data.edges || [];
    if (!claims.length) { canvas.dataset.fluid = "empty"; return; }

    var A = window.ALIBI, reduced = A && A.reduced();
    var gl = null;
    try {
      gl = canvas.getContext("webgl2", { alpha: true, antialias: false, depth: false, premultipliedAlpha: false });
    } catch (e) { gl = null; }
    if (!gl) { canvas.dataset.fluid = "no-webgl2"; return; }

    /* ---- layout: one ring per subject, height from how soon the obligation bites ---- */
    var bySubject = {};
    claims.forEach(function (c) { (bySubject[c.s] = bySubject[c.s] || []).push(c); });
    var subjects = Object.keys(bySubject).sort();
    var layout = {}, maxDays = 1;
    claims.forEach(function (c) {
      if (c.d !== null && c.d !== undefined) maxDays = Math.max(maxDays, Math.abs(c.d));
    });
    subjects.forEach(function (s, si) {
      var group = bySubject[s], ang = (si / subjects.length) * Math.PI * 2;
      group.forEach(function (c, ci) {
        var spread = (ci - (group.length - 1) / 2) * 0.22;
        var soon = c.d === null || c.d === undefined ? 0 : (1 - Math.min(1, c.d / maxDays));
        var r = 1.05 + (c.a === undefined ? 0 : (0.5 - c.a)) * 0.55 + ci * 0.055;
        layout[c.id] = [Math.cos(ang + spread) * r, (soon - 0.5) * 1.5 + (ci % 3) * 0.14,
                        Math.sin(ang + spread) * r];
      });
    });

    var n = claims.length, node = new Float32Array(n * 8);
    var order = claims.map(function (c) { return c.id; });
    var idIndex = {}; order.forEach(function (id, i) { idIndex[id] = i; });
    claims.forEach(function (c, i) {
      var L = layout[c.id] || [0, 0, 0];
      var mode = c.k === "conflict" ? 2 : (c.k === "review" ? 1 : 0);
      var hue = c.k === "conflict" ? 0.9 : (c.k === "review" ? 0.5 : 0.1);
      node[i * 8 + 0] = L[0]; node[i * 8 + 1] = L[1]; node[i * 8 + 2] = L[2];
      node[i * 8 + 3] = 0.052; node[i * 8 + 4] = 0; node[i * 8 + 5] = 0;
      node[i * 8 + 6] = hue; node[i * 8 + 7] = mode;
    });
    // the two-triangle quad every billboard instance shares
    var quad = new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]);
    var line = new Float32Array(Math.max(6, edges.length * 2 * 4));

    function shader(type, src) {
      var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        console.warn("[alibi] scene shader refused: " + gl.getShaderInfoLog(s)); return null; }
      return s;
    }
    function link(vs, fs, names, out) {
      var a = shader(gl.VERTEX_SHADER, vs), b = shader(gl.FRAGMENT_SHADER, fs);
      if (!a || !b) return null;
      var p = gl.createProgram(); gl.attachShader(p, a); gl.attachShader(p, b); gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) { console.warn("[alibi] scene link: " + gl.getProgramInfoLog(p)); return null; }
      names.forEach(function (k) { out[k] = gl.getUniformLocation(p, k); });
      return p;
    }
    var nloc = {}, lloc = {};
    var nProg = link(NODE_VS, NODE_FS, ["uProj", "uView", "uModel", "uPx", "uGround", "uReview", "uConflict"], nloc);
    var lProg = link(LINE_VS, LINE_FS, ["uProj", "uView", "uModel", "uTint"], lloc);
    if (!nProg || !lProg) { canvas.dataset.fluid = "compile-failed"; return; }

    var nodeBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuf); gl.bufferData(gl.ARRAY_BUFFER, node, gl.STATIC_DRAW);
    var quadBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf); gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);
    var lineBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, lineBuf); gl.bufferData(gl.ARRAY_BUFFER, line, gl.DYNAMIC_DRAW);

    var nodeVao = gl.createVertexArray();
    gl.bindVertexArray(nodeVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuf);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 32, 0); gl.vertexAttribDivisor(0, 1);
    gl.enableVertexAttribArray(2); gl.vertexAttribPointer(2, 2, gl.FLOAT, false, 32, 24); gl.vertexAttribDivisor(2, 1);
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
    gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 8, 0); gl.vertexAttribDivisor(1, 0);
    var lineVao = gl.createVertexArray();
    gl.bindVertexArray(lineVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, lineBuf);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 16, 0);
    gl.bindVertexArray(null);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    var proj = mat4(), view = mat4(), model = mat4(), mvp = mat4();
    var W = 0, H = 0, spin = 0, tiltY = 0.16, pick = -1, hover = -1, fade = 1;
    function resize() {
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      var r = canvas.getBoundingClientRect();
      var w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
      if (w === W && h === H) return;
      W = w; H = h; canvas.width = w; canvas.height = h;
      perspective(proj, 0.85, w / h, 0.1, 30);
    }
    // Two passes: the first paints at whatever size layout has settled on, the second catches a stylesheet
    // that arrived after `DOMContentLoaded` and changed the box (it cost a 300×150 default earlier, i.e. a
    // field drawn in the top-left corner of a 650 px canvas).
    resize(); frame(16.7); resize();
    setTimeout(function () { resize(); frame(16.7); }, 60);
    window.addEventListener("resize", function () { resize(); if (reduced) frame(16.7); }, { passive: true });

    function multiply(o, a, b) {
      for (var c = 0; c < 4; c++) for (var r2 = 0; r2 < 4; r2++) {
        o[c * 4 + r2] = a[r2] * b[c * 4] + a[4 + r2] * b[c * 4 + 1] + a[8 + r2] * b[c * 4 + 2] + a[12 + r2] * b[c * 4 + 3];
      }
      return o;
    }

    function frame(dt) {
      var sc = A && A.scroll ? A.scroll() : { vel: 0, y: 0 };
      var pt = A && A.pointer ? A.pointer : { x: 0.5, y: 0.5 };
      if (!reduced) {
        spin += dt * 0.00007 * (1 + Math.min(2.6, Math.abs(sc.vel) / 22));   // scroll velocity drives the world
        tiltY += ((pt.y - 0.5) * 0.5 - tiltY) * 0.05;
      }
      modelMatrix(model, spin, tiltY);
      viewMatrix(view, 0, 0, -3.35);
      multiply(mvp, proj, multiply(mat4(), view, model));
      resize();

      gl.viewport(0, 0, W, H);
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);

      // edges first (they sit behind the nodes), rebuilt from the same projection the nodes use
      for (var e = 0; e < edges.length; e++) {
        var a = layout[edges[e].f], b = layout[edges[e].t];
        if (!a || !b) continue;
        line[e * 8 + 0] = a[0]; line[e * 8 + 1] = a[1]; line[e * 8 + 2] = a[2]; line[e * 8 + 3] = 1;
        line[e * 8 + 4] = b[0]; line[e * 8 + 5] = b[1]; line[e * 8 + 6] = b[2]; line[e * 8 + 7] = 1;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, lineBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, line.subarray(0, edges.length * 8));
      gl.useProgram(lProg);
      gl.uniformMatrix4fv(lloc.uProj, false, proj); gl.uniformMatrix4fv(lloc.uView, false, view);
      gl.uniformMatrix4fv(lloc.uModel, false, model);
      gl.uniform3f(lloc.uTint, 0.55, 0.72, 0.95);
      gl.bindVertexArray(lineVao);
      gl.drawArrays(gl.LINES, 0, edges.length * 2);

      gl.useProgram(nProg);
      gl.uniformMatrix4fv(nloc.uProj, false, proj); gl.uniformMatrix4fv(nloc.uView, false, view);
      gl.uniformMatrix4fv(nloc.uModel, false, model);
      gl.uniform1f(nloc.uPx, H * 0.5 * (reduced ? 1 : fade));
      gl.uniform3f(nloc.uGround, 0.36, 0.85, 0.60);
      gl.uniform3f(nloc.uReview, 1.00, 0.74, 0.32);
      gl.uniform3f(nloc.uConflict, 1.00, 0.42, 0.42);
      gl.bindVertexArray(nodeVao);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, n);

      if (hover >= 0) {
        var s = project(mvp, layout[order[hover]][0], layout[order[hover]][1], layout[order[hover]][2], W, H);
        host.style.setProperty("--tipx", (s[0] / (window.devicePixelRatio || 1)).toFixed(0) + "px");
        host.style.setProperty("--tipy", (s[1] / (window.devicePixelRatio || 1)).toFixed(0) + "px");
      }
      if (!A || !A.Engine) return;
    }

    var screen = [];
    function nearest(clientX, clientY) {
      var r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      var x = (clientX - r.left) * dpr, y = (clientY - r.top) * dpr, best = -1, bd = 26 * dpr;
      for (var i = 0; i < n; i++) {
        var L = layout[order[i]];
        if (!L) continue;
        var s = project(mvp, L[0], L[1], L[2], W, H);
        var d = Math.hypot(s[0] - x, s[1] - y);
        if (d < bd) { bd = d; best = i; }
      }
      screen = best >= 0 ? project(mvp, layout[order[best]][0], layout[order[best]][1], layout[order[best]][2], W, H) : [];
      return best;
    }
    canvas.addEventListener("pointermove", function (ev) {
      var i = nearest(ev.clientX, ev.clientY);
      if (i !== hover) {
        hover = i;
        host.dataset.hover = i >= 0 ? "1" : "0";
        var tip = host.querySelector(".scene-tip");
        if (tip) {
          if (i >= 0) {
            var c = claims[i];
            tip.textContent = c.p + " · " + c.v + (c.d === null || c.d === undefined ? "" : " · " + c.d + "d out");
            tip.dataset.state = c.k;
          } else { tip.textContent = ""; }
        }
      }
      if (reduced) frame(16.7);
      if (A && A.Engine) A.Engine.wake();
    }, { passive: true });
    canvas.addEventListener("click", function (ev) {
      var i = nearest(ev.clientX, ev.clientY);
      if (i >= 0 && claims[i].u) location.href = claims[i].u;
    });
    canvas.addEventListener("pointerleave", function () {
      hover = -1; host.dataset.hover = "0";
      var tip = host.querySelector(".scene-tip"); if (tip) tip.textContent = "";
    });
    document.addEventListener("keydown", function (ev) {
      var link = ev.target.closest && ev.target.closest(".scene-index a");
      if (!link) return;
      var id = parseInt(link.dataset.claim, 10);
      hover = order.indexOf(isNaN(id) ? -1 : id);
      if (reduced) frame(16.7);
    });

    // A debug handle for the harness. Note what it does NOT claim: `coverage` is deliberately absent,
    // because `readPixels` on a `preserveDrawingBuffer: false` back buffer is undefined once the frame has
    // been handed to the compositor — it read pure black on a field that every screenshot showed painted.
    // So this handle reports the things a page can honestly know (geometry, GL error, whether the context
    // died) and the harness proves pixels exist by rasterising the element itself.
    window.__alibiScene = {
        gl: function () { return gl; }, canvas: canvas, nodes: function () { return n; },
        // Harness-only: report whether the field is currently in the engine's loop. A paused engine means the
        // compositor is holding the last cleared frame, which is exactly what makes a naive screenshot of a
        // `preserveDrawingBuffer: false` canvas come back black — see the paint check in verify-fluid.mjs.
        cadence: function () { return A && A.Engine ? A.Engine.fps() : 0; },
        edges: function () { return edges.length; }, size: function () { return [canvas.width, canvas.height]; },
        program: function () { return { nodes: !!nProg, lines: !!lProg }; },
        err: function () { return gl.getError(); }, lost: function () { return gl.isContextLost(); }
    };
    if (A && A.Engine) { A.Engine.add(frame); }
    else { (function loop() { frame(16.7); requestAnimationFrame(loop); })(); }
    canvas.dataset.fluid = "live";
  }

  if (document.readyState !== "loading") boot(); else document.addEventListener("DOMContentLoaded", boot);
})();
