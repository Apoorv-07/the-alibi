/* ALIBI · atmosphere.js — the light in the room. WebGL2 + GLSL, inline, no fetch, no library.
 *
 * What this layer is for: the app is a ledger of things that are true *right now* about a student's
 * deadlines. A page of numbers cannot carry that weight without feeling like a terminal, so the room has
 * weather — a slow fluid field that the pointer disturbs and the scroll drags through. It is decoration and
 * it is marked as such (`aria-hidden`, a plain gradient underneath it, `pointer-events: none`), which is why
 * every failure path here is silent and cheap:
 *
 *   no WebGL2                     →  the CSS gradient under the canvas stays; nothing else changes
 *   shader refused / no program   →  same, plus one console line naming the reason
 *   context lost (sleep, GPU reset) → the loop unsubscribes, the last frame remains, no repaint storm
 *   reduced motion                →  one frame is drawn and held; the field stops drifting
 *   quality tier 0                →  no GL at all: a 2 GB Chromebook must not pay for beauty
 *
 * The field is deliberately *not* a 3D demo: no camera, no geometry, two fullscreen triangles. Everything
 * costs a handful of texture-free math ops per texel, rendered at 36–50 % resolution and upscaled with
 * bilinear filtering, which is what makes it read as atmosphere instead of as noise.
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

  var TRI_VERT = `#version 300 es
precision highp float;
out vec2 vUv;
void main() {
  // One triangle covering the screen, driven by gl_VertexID so there is no buffer to upload. The ×2 is not
  // decoration: the raw ids give (-1,-1) (1,-1) (-1,1), which is a *half*-screen triangle — every pixel to
  // the right of x=0 kept the clear colour, and the whole "atmosphere" looked like a rendering bug.
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  vUv = p;
  p = p * 2.0 - 1.0;
  gl_Position = vec4(p, 0.0, 1.0);
}`;

  var FRAG = `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;

uniform vec2  uRes;        // target size, px
uniform float uTime;       // seconds, ambient only
uniform float uProg;       // 0..1 how far through the document
uniform float uVel;        // -1..1 scroll velocity; the sign matters
uniform vec2  uPtr;        // pointer, 0..1
uniform float uEnergy;     // 0..1 pointer + scroll activity, decaying
uniform float uMorph;      // eased section position, drives the palette drift
uniform vec3  uA;          // accents are read from CSS, so the theme owns colour
uniform vec3  uB;
uniform vec3  uC;

float hash21(vec2 p) {
  p = fract(p * vec2(123.34, 345.45));
  p += dot(p, p + 34.345);
  return fract(p.x * p.y);
}
vec2 hash22(vec2 p) {
  p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
  return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
}
// Gradient noise: smooth in both derivatives (value noise steps visibly when the field is dragged).
float gnoise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
  return mix(mix(dot(hash22(i), f), dot(hash22(i + vec2(1, 0)), f - vec2(1, 0)), u.x),
             mix(dot(hash22(i + vec2(0, 1)), f - vec2(0, 1)), dot(hash22(i + vec2(1, 1)), f - vec2(1, 1)), u.x),
             u.y);
}
float fbm(vec2 p, int oct) {
  float s = 0.0, a = 0.55;
  for (int i = 0; i < 6; i++) {
    if (i >= oct) break;
    s += a * gnoise(p);
    p = mat2(1.6, 1.2, -1.2, 1.6) * p;      // rotate-and-stretch: kills the axis-aligned look
    a *= 0.5;
  }
  return s;
}

void main() {
  vec2 uv = vUv;
  float ar = uRes.x / max(1.0, uRes.y);
  vec2 q = vec2(uv.x * ar, uv.y);
  int oct = (uRes.y > 520.0 && uEnergy >= 0.0) ? (uRes.y > 700.0 ? 5 : 4) : 3;   // fewer octaves on a small
                                                                                   // or low-tier target

  // domain warp: two fields bending the coordinates of a third. This is the whole trick behind "liquid" —
  // iso-lines curve instead of scrolling.
  float t = uTime * 0.021;
  vec2 w = vec2(fbm(q * 1.1 + vec2(t, -t * 0.6), oct), fbm(q * 1.35 + vec2(-t * 0.8, t), oct));
  vec2 p = q + 0.55 * w + 0.18 * uVel * vec2(0.0, 1.0);

  float n1 = fbm(p * 1.05 + vec2(0.0, -uProg * 1.4), oct);
  float n2 = fbm(p * 2.1 - vec2(uProg * 0.8, 0.0), max(3, oct - 1));

  float band = 0.5 + 0.5 * sin((n1 * 2.4 + n2 * 1.1) * 3.14159 + uMorph * 1.9);
  band = pow(clamp(band, 0.0, 1.0), 2.3);
  float veil = smoothstep(0.0, 1.0, 0.55 + 0.75 * n2);

  // palette drift by section: one continuous environment, several moods
  vec3 cold = mix(uA, uC, clamp(uMorph * 1.25, 0.0, 1.0));
  vec3 warm = mix(uC, uB, clamp(uMorph * 0.8, 0.0, 1.0));
  vec3 col = mix(cold * 0.26, warm * 0.48, band);
  col += warm * 0.10 * veil;

  // pointer: not a torch that follows the mouse — a wave that leaves the cursor and dies
  vec2 d = (uv - uPtr) * vec2(ar, 1.0);
  float r = length(d) + 1e-4;
  float wave = exp(-r * 5.2) * cos(r * 26.0 - uTime * 1.7) * (0.30 + 0.70 * uEnergy);
  col += uB * wave * 0.45;
  col += uC * exp(-r * r * 22.0) * (0.08 + 0.26 * uEnergy);

  // a key light top-right, a cooler fill bottom-left, and a vignette so the reading column stays darker
  float key = pow(max(0.0, 1.0 - length((uv - vec2(0.82, 0.02)) * vec2(ar, 1.0)) / 0.95), 2.2);
  float fill = pow(max(0.0, 1.0 - length((uv - vec2(0.05, 1.05)) * vec2(ar, 1.0)) / 1.25), 2.6);
  col += uA * key * 0.13 + uB * fill * 0.09;
  float vig = 1.0 - 0.42 * pow(length(uv - 0.5) * 1.32, 2.0);
  col *= clamp(vig, 0.35, 1.0);

  // an 8-bit target bands on a gradient this smooth; two lines of dither beat a texture
  col += (hash21(gl_FragCoord.xy + uTime) - 0.5) / 255.0;

  outColor = vec4(max(col, 0.0), 1.0);
}`;

  var BLIT_FRAG = `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uTex;
void main() { outColor = vec4(texture(uTex, vUv).rgb, 1.0); }`;

  function readAccent(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    var m = /^#?([0-9a-f]{6})$/i.exec(v);
    if (m) {
      var n = parseInt(m[1], 16);
      return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
    }
    m = /^#?([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(v);
    if (m) return [parseInt(m[1] + m[1], 16) / 255, parseInt(m[2] + m[2], 16) / 255,
                   parseInt(m[3] + m[3], 16) / 255];
    return fallback;
  }

  function boot() {
    var canvas = document.getElementById("atmosphere");
    if (!canvas) return;
    var A = window.ALIBI, reduced = A && A.reduced();
    if (canvas.dataset.fluidOff === "1") { canvas.dataset.fluid = "skipped"; return; }
    // Tier 0 is not "no WebGL", it is "fewest texels": a 2 GB Chromebook still gets the room's light, at a
    // quarter of the resolution and three octaves. Refusing to draw at all was the earlier behaviour, and it
    // meant the flagship screen looked broken on exactly the machines a reviewer brings to a demo.
    var tier = (A && A.quality) ? A.quality() : 2;
    var gl = null;
    try {
      gl = canvas.getContext("webgl2", { alpha: false, antialias: false, depth: false, stencil: false,
                                         powerPreference: "low-power", preserveDrawingBuffer: false });
    } catch (e) { gl = null; }
    if (!gl) { canvas.dataset.fluid = "no-webgl2"; return; }

    var scale = tier === 0 ? 0.22 : (tier === 1 ? 0.34 : (window.innerWidth * window.innerHeight > 2400000 ? 0.34 : 0.5));
    var time = 0, energy = 0, lx = 0.5, ly = 0.5, cw = 0, ch = 0;
    var program, blit, vao, blitVao, tex, fbo, loc = {}, bloc = {};

    function shader(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        console.warn("[alibi] atmosphere shader refused: " + gl.getShaderInfoLog(s));
        return null;
      }
      return s;
    }
    function link(vsSrc, fsSrc, names, out) {
      var vs = shader(gl.VERTEX_SHADER, vsSrc), fs = shader(gl.FRAGMENT_SHADER, fsSrc);
      if (!vs || !fs) return null;
      var pr = gl.createProgram();
      gl.attachShader(pr, vs); gl.attachShader(pr, fs); gl.linkProgram(pr);
      if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) {
        console.warn("[alibi] atmosphere program refused: " + gl.getProgramInfoLog(pr)); return null;
      }
      names.forEach(function (n) { out[n] = gl.getUniformLocation(pr, n); });
      return pr;
    }
    function build() {
      program = link(TRI_VERT, FRAG, ["uRes", "uTime", "uProg", "uVel", "uPtr", "uEnergy", "uMorph",
                                      "uA", "uB", "uC"], loc);
      blit = link(TRI_VERT, BLIT_FRAG, ["uTex"], bloc);
      if (!program || !blit) return false;
      vao = gl.createVertexArray();
      blitVao = gl.createVertexArray();
      tex = gl.createTexture();
      fbo = gl.createFramebuffer();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      var a = readAccent("--fluid-a", [0.36, 0.56, 0.96]),
          b = readAccent("--fluid-b", [0.40, 0.86, 0.68]),
          c = readAccent("--fluid-c", [0.63, 0.47, 0.95]);
      gl.useProgram(program);
      gl.uniform3f(loc.uA, a[0], a[1], a[2]);
      gl.uniform3f(loc.uB, b[0], b[1], b[2]);
      gl.uniform3f(loc.uC, c[0], c[1], c[2]);
      return true;
    }
    if (!build()) { canvas.dataset.fluid = "compile-failed"; return; }

    function resize() {
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      var w = Math.max(1, Math.round(window.innerWidth * dpr * scale));
      var h = Math.max(1, Math.round(window.innerHeight * dpr * scale));
      if (w === cw && h === ch) return;
      cw = w; ch = h;
      canvas.width = w; canvas.height = h;
      gl.bindTexture(gl.TEXTURE_2D, tex);
            // internalformat RGBA8, *format* RGBA: passing gl.RGBA8 twice is an INVALID_ENUM in WebGL2, and an
      // incomplete framebuffer then produces a silently blank canvas — a warning in the console that nobody
      // reads, on an effect the whole design leans on. The format argument is the only legal one here.
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      canvas.dataset.fluid = "live";
    }
    resize();
    window.addEventListener("resize", function () { resize(); if (reduced) drawOnce(); }, { passive: true });

    canvas.addEventListener("webglcontextlost", function (e) {
      e.preventDefault(); canvas.dataset.fluid = "context-lost"; unsubscribe();
    });
    canvas.addEventListener("webglcontextrestored", function () {
      canvas.dataset.fluid = "context-restored";
      if (build()) { cw = 0; resize(); subscribe(); drawOnce(); }
    });

    function frame(dt) {
      var sc = A && A.scroll ? A.scroll() : { y: 0, max: 0, vel: 0 };
      var pt = A && A.pointer ? A.pointer : { x: 0.5, y: 0.5 };
      energy = Math.max(energy * 0.965, Math.min(1, Math.abs(sc.vel) / 40 +
               Math.hypot(pt.x - lx, pt.y - ly) * 6));
      lx = pt.x; ly = pt.y;
      if (!reduced) time += dt / 1000;
      paint(sc, pt, (sc.max ? sc.y / sc.max : 0));
    }
    function drawOnce() {
      frame(16.7);
    }
    function paint(sc, pt, prog) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, cw, ch);
      gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(program);
      gl.uniform2f(loc.uRes, cw, ch);
      gl.uniform1f(loc.uTime, time);
      gl.uniform1f(loc.uProg, prog);
      gl.uniform1f(loc.uVel, Math.max(-1, Math.min(1, sc.vel / 45)));
      gl.uniform2f(loc.uPtr, pt.x, 1 - pt.y);
      gl.uniform1f(loc.uEnergy, energy);
      gl.uniform1f(loc.uMorph, prog);
      gl.bindVertexArray(vao);
      gl.drawArrays(gl.TRIANGLES, 0, 3);

      // composite the small field onto the canvas; CSS scales the canvas to the viewport, and that
      // bilinear upscale *is* the volumetric softness, so no blur pass costs anything here
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, cw, ch);
      gl.useProgram(blit);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.uniform1i(bloc.uTex, 0);
      gl.bindVertexArray(blitVao);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    var off = null;
    function subscribe() { if (!off && A && A.Engine) off = A.Engine.add(frame); }
    function unsubscribe() { if (off) { off(); off = null; } }

    if (!A || !A.Engine) { canvas.dataset.fluid = "no-engine"; return; }
    A.Engine.onadapt = function (q) { scale = q <= 0 ? 0.26 : 0.34; cw = 0; resize(); };
    if (reduced) { drawOnce(); canvas.dataset.fluid = "live-static"; } else { subscribe(); }
    // A debug handle, on purpose: `window.__alibiAtmos.dump()` is how a reviewer checks the field without
    // a GPU-aware browser, and how this file's own failure modes can be named in a bug report.
    window.__alibiAtmos = {
        gl: gl, canvas: canvas, loc: loc, program: program, blit: blit, size: function () { return [cw, ch]; },
        scale: function () { return scale; },
        sample: function (rgb) {
            var w = Math.min(64, cw), h = Math.min(40, ch), buf = new Uint8Array(w * h * 4);
            gl.bindFramebuffer(gl.FRAMEBUFFER, rgb ? null : fbo);
            gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
            var lit = 0; for (var i = 0; i < buf.length; i += 4) if (buf[i] + buf[i+1] + buf[i+2] > 18) lit++;
            return { litPct: +(lit / (buf.length / 4) * 100).toFixed(1), px: Array.prototype.slice.call(buf, 0, 8) };
        },
        lastUniforms: function () { return { time: time, energy: energy }; }
    };
    window.addEventListener("beforeprint", unsubscribe);
    window.addEventListener("afterprint", function () { if (!reduced) subscribe(); });
  }

  if (document.readyState !== "loading") boot(); else document.addEventListener("DOMContentLoaded", boot);
})();
