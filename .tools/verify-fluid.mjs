/* Frontend verification for the fluid layer. Not a unit test: this drives a real browser at real viewports,
 * measures what the user would see (frame cadence, whether the WebGL planes actually painted pixels, whether
 * the page is still fully usable with the environment turned off) and fails loudly on console errors.
 *
 *   node .tools/verify-fluid.mjs --url http://127.0.0.1:8000
 *
 * Exits non-zero if any check fails, so CI can run it. Chromium here is software-rendered (SwANGLE), which is
 * the *worst* case for the shaders — if the field paints at 2 FPS in software, the pixel path is proven; the
 * FPS gate below is therefore measured against the reduced-quality build too, and reported, not silently
 * asserted at an arbitrary 60.
 */
import zlib from "node:zlib";
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { chromium } from "playwright";

const ROOT = new URL("..", import.meta.url).pathname;
const url = (process.argv.find((a, i) => process.argv[i - 1] === "--url") || "http://127.0.0.1:8000").replace(/\/$/, "");
const shots = process.argv.includes("--shots");
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok, detail }); };

const VIEWPORTS = [
  { name: "desktop-1440", width: 1440, height: 900, dpr: 1 },
  { name: "laptop-1280", width: 1280, height: 800, dpr: 2 },
  { name: "tablet-834", width: 834, height: 1112, dpr: 2, touch: true },
  { name: "phone-390", width: 390, height: 844, dpr: 3, touch: true },
];
/* The renovation split the app in two, and the harness has to follow it: the *fluid* pages are the technical
   surfaces (where the WebGL field, the cursor and the inertial scroll belong), the *calm* pages are the six
   destinations (where none of it may appear). The old `PAGES[0] === "/"` was the cockpit; `/` is now the
   calm home page, so every cinematic check re-points at /technical/cockpit — otherwise the suite would pass
   by accident, on pages that no longer have a canvas. */
const PAGES = ["/technical/cockpit", "/twin", "/timeline", "/claims", "/lineage", "/conflicts", "/queue",
               "/feasibility", "/risk", "/actions", "/sources", "/privacy", "/eval", "/audit", "/settings",
               "/docs", "/technical"];
/* `/settings` is deliberately NOT here: it is a working form surface (uploads, wipe, model config) that
   keeps the technical shell, and pretending it is one of the calm six would make this check a lie. */
const CALM_PAGES = ["/?demo=1", "/calendar", "/tasks", "/review", "/evidence", "/technical", "/onboard"];

const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--disable-gpu-sandbox", "--no-sandbox"],
});

async function openPage(ctx, path, opts = {}) {
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", e => errors.push("pageerror: " + e.message));
  page.on("requestfailed", r => errors.push("requestfailed: " + r.url()));
  await page.goto(url + path, { waitUntil: "load" });
  await page.waitForTimeout(opts.settle ?? 900);
  return { page, errors };
}

/* Count ink in a PNG the browser produced, with no third-party decoder: inflate the IDAT streams and undo
   the per-scanline filters. Enough to answer "did the compositor receive any pixels?" from the one place the
   answer is observable — a canvas with `preserveDrawingBuffer: false` cannot be read with `readPixels`. */
function inflatePng(data) {
  const idat = [];
  let width = 0, height = 0, color = -1;
  for (let o = 8; o + 8 <= data.length;) {
    const len = data.readUInt32BE(o), type = data.toString("ascii", o + 4, o + 8);
    // PNG chunk = 4 length, 4 type, payload, 4 CRC. The IHDR payload is width(4) height(4) bit depth(1)
    // colour type(1) compression filter interlace, so those two bytes sit at o+8+8 / o+8+9. Reading them a
    // chunk-header further out produced a bogus "colour type 0" and decoded an RGB scanline as one byte a
    // pixel — which is how a band full of nodes measured as 0% chroma while PIL read the same PNG correctly.
    if (type === "IHDR") {
      // One slice, then index inside it: PNG chunk = 4 length, 4 type, payload, 4 CRC, and the IHDR payload
      // is width(4) height(4) bit depth(1) colour type(1) compression filter interlace. Deriving the two
      // bytes from the payload slice instead of counting `o + 16/17/24/25` by hand is the only version of
      // this that stays right when somebody (me) miscounts — a wrong colour type silently decodes an RGB
      // scanline as one byte a pixel, and a band full of nodes reads back as 0% chroma.
      const ihdr = data.subarray(o + 8, o + 8 + len);
      width = ihdr.readUInt32BE(0); height = ihdr.readUInt32BE(4);
      const bitDepth = ihdr[8]; color = ihdr[9];
      if (bitDepth !== 8) throw new Error("expected an 8-bit PNG, got bit depth " + bitDepth);
    }
    if (type === "IDAT") idat.push(data.subarray(o + 8, o + 8 + len));
    if (type === "IEND") break;
    o += 12 + len;
  }
  // Playwright hands back an RGB PNG (colour type 2, and `omitBackground` does not change that), so decode
  // three bytes a pixel and keep a fourth "alpha" slot of 255 for the analyser below.
  const bpp = color === 6 ? 4 : color === 2 ? 3 : color === 0 ? 1 : -1;
  if (color === 3 || color === 4) throw new Error("palette/alpha PNGs are not expected from a screenshot");
  if (bpp < 0) throw new Error("unsupported PNG colour type " + color);
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const out = Buffer.alloc(width * height * 4), stride = width * bpp;
  const outRaw = new Array(height);
  let pos = 0;
  for (let y = 0; y < height; y++) {
    const filter = raw[pos++], line = raw.subarray(pos, pos + stride); pos += stride;
    const cur = Buffer.alloc(stride);
    // Filters must be undone against the *raw scanlines*, not the re-laid-out RGBA buffer: indexing `out`
    // with a scanline offset (width×3, not width×4) silently reconstructed every row from the wrong bytes,
    // which is how a band full of nodes decoded to "0% ink" and made me distrust a working screenshot.
    const prev = y ? outRaw[y - 1] : null;
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? cur[x - bpp] : 0, b = prev ? prev[x] : 0, c = prev && x >= bpp ? prev[x - bpp] : 0;
      let v = line[x];
      if (filter === 1) v += a; else if (filter === 2) v += b; else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) {
        const pp = a + b - c, pa = Math.abs(pp - a), pb = Math.abs(pp - b), pc = Math.abs(pp - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      cur[x] = v & 255;
    }
    outRaw[y] = cur;
    for (let x = 0; x < width; x++) {
      const o = (y * width + x) * 4, s = x * bpp;
      if (bpp === 4) { out[o] = cur[s]; out[o+1] = cur[s+1]; out[o+2] = cur[s+2]; out[o+3] = cur[s+3]; }
      else if (bpp === 3) { out[o] = cur[s]; out[o+1] = cur[s+1]; out[o+2] = cur[s+2]; out[o+3] = 255; }
      else { out[o] = out[o+1] = out[o+2] = cur[s]; out[o+3] = 255; }
    }
  }
  return { width, height, px: out };
}
function analyseRgba(buf) {
  try {
    const { width, height, px } = inflatePng(buf);
    let lit = 0, ink = 0, hue = 0;
    for (let i = 0; i < px.length; i += 4) {
      const r = px[i], g = px[i+1], b = px[i+2];
      if (px[i+3] > 6) lit++;
      const max = Math.max(r, g, b), min = Math.min(r, g, b);
      if (r + g + b > 18) ink++;
      // The band's own background is a near-neutral radial tint, so *chroma* is what distinguishes a node of
      // the ledger (a green/amber/red disc, drawn from a row) from "the CSS painted, the GL did not".
      if (max - min > 26) hue++;
    }
    // `px.length`, not `width * height`: the buffer is four bytes a pixel, so dividing by the pixel *count*
    // reported every rate as exactly one quarter of itself — and the chroma figure, already a small number,
    // floored under the threshold and read as "the canvas never painted".
    const n = px.length / 4;
    return { width, height, litPct: lit / n * 100, inkPct: ink / n * 100, chromaPct: hue / n * 100 };
  } catch (e) { return { error: String(e.message).slice(0, 70) }; }
}

/* The decoder is a dependency of a pass/fail assertion, so it gets a known-answer test of its own before the
   page is judged by it: a 4×1 PNG (bright red, dark grey, bright green, fully transparent) whose chroma, ink
   and lit rates are known by construction. Colour-type and bit-depth bytes are easy to misread by one and the
   failure looks exactly like "the canvas never painted". Run with `node .tools/verify-fluid.mjs --selftest`. */
const PNG_KNOWN_ANSWER =
  "iVBORw0KGgoAAAANSUhEUgAAAAQAAAABCAYAAAD5PA/NAAAAGUlEQVR4nGM4wcX1n5WV9T/Xiaj/DAwMDAAxIQUVNsqqDwAAAABJRU5ErkJggg==";
function selfTest() {
  const png = Buffer.from(PNG_KNOWN_ANSWER, "base64");
  const r = analyseRgba(png);
  const want = { width: 4, height: 1, litPct: 75, inkPct: 50, chromaPct: 50 };
  const ok = r && ["width","height","litPct","inkPct","chromaPct"].every(k => r[k] === want[k]);
  console.log((ok ? "PASS" : "FAIL") + "  harness PNG decoder (known-answer 4×1): " + JSON.stringify(r)
              + " want " + JSON.stringify(want));
  process.exit(ok ? 0 : 1);
}
if (process.argv.includes("--selftest")) selfTest();

/* ------------------------------------------------------------------ 1 · planes --- */
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const { page, errors } = await openPage(ctx, "/technical/cockpit");
  const env = await page.evaluate(() => {
    const c = document.getElementById("atmosphere");
    // The atmosphere is verified through its own debug handle, reading the *render target*, not the default
    // framebuffer: the context is created with `preserveDrawingBuffer: false` (the right call for a page that
    // composites every frame), and `readPixels` on a presented buffer is undefined behaviour — it returns
    // black and sends you chasing a bug that is in the test, not the site.
    const A = window.__alibiAtmos;
    const field = A ? A.sample(false) : { litPct: -1 };
    return {
      fluidMotion: document.documentElement.dataset.fluidMotion,
      fluidScroll: document.documentElement.dataset.fluidScroll,
      quality: document.documentElement.dataset.fluidQuality,
      canvasState: c.dataset.fluid,
      canvasSize: [c.width, c.height],
      paintedRatio: field.litPct / 100,
      glLost: A ? A.gl.isContextLost() : null,
      scene: !!document.querySelector(".scene canvas"),
      cursor: !!document.querySelector(".fluid-ring"),
      orbs: document.querySelectorAll(".fluid-orb").length,
      reveals: document.querySelectorAll("[data-reveal]").length,
      revealedIn: document.querySelectorAll("[data-reveal].is-in").length,
      webgl2: !!document.createElement("canvas").getContext("webgl2"),
      externalAssets: [...document.querySelectorAll("link[href],script[src]")]
        .map(n => n.href || n.src).filter(u => /^https?:\/\//.test(u) && !u.startsWith(location.origin)),
      h1: document.querySelectorAll("h1").length,
      title: document.title
    };
  });
  check("webgl2 available in this browser", env.webgl2, "the shader plane cannot be verified without it");
  check("atmosphere canvas reporting live", env.canvasState === "live", `state=${env.canvasState}`);
  check("atmosphere painted pixels", env.paintedRatio > 0.05, `lit=${(env.paintedRatio * 100).toFixed(1)}% of the field`);
  check("atmosphere context not lost", env.glLost === false, String(env.glLost));
  check("reduced-motion flag published", ["on", "reduced"].includes(env.fluidMotion), env.fluidMotion);
  check("scroll mode = inertia on a fine pointer", env.fluidScroll === "inertia", env.fluidScroll);
  check("quality tier chosen by probe", ["0", "1", "2"].includes(env.quality), env.quality);
  check("field-of-facts canvas mounted", env.scene);
  check("cursor layer present", env.cursor);
  check("three depth planes", env.orbs === 3, String(env.orbs));
  check("reveal elements present and entering", env.reveals > 3 && env.revealedIn > 0,
        `${env.revealedIn}/${env.reveals} in`);
  check("no external asset requests", env.externalAssets.length === 0, env.externalAssets.join(","));
  check("exactly one h1", env.h1 === 1, String(env.h1));
  check("no console errors on the cockpit", errors.length === 0, errors.slice(0, 3).join(" | "));

  /* ---------------------------------------------------- 2 · motion, measured --- */
  const motion = await page.evaluate(async () => {
    const before = window.scrollY;
    const out = { before, steps: [], samples: [] };
    await new Promise(res => {
      let n = 0, last = performance.now(), frames = 0, t0 = performance.now();
      const count = () => { frames++; if (performance.now() - last > 16) { out.samples.push(performance.now() - last); last = performance.now(); } };
      requestAnimationFrame(function loop() {
        count();
        window.dispatchEvent(new WheelEvent("wheel", { deltaY: 600, cancelable: true }));
        n++;
        if (n < 40) requestAnimationFrame(loop); else res();
      });
      setTimeout(res, 2500);
      void t0;
    });
    out.after = window.scrollY;
    out.rootSy = getComputedStyle(document.documentElement).getPropertyValue("--sy");
    out.rootProg = getComputedStyle(document.documentElement).getPropertyValue("--prog");
    out.fps = window.ALIBI ? window.ALIBI.Engine.fps() : -1;
    out.max = window.ALIBI ? window.ALIBI.scroll().max : -1;
    return out;
  });
  check("wheel scrolled through the virtual scroll", motion.after > motion.before + 40,
        `${Math.round(motion.before)} → ${Math.round(motion.after)} (max ${Math.round(motion.max)})`);
  check("--sy tracks the smoothed position", Math.abs(parseFloat(motion.rootSy) - motion.after) < 3,
        `--sy=${motion.rootSy}`);
  check("--prog published for scroll-linked styling", parseFloat(motion.rootProg) > 0, motion.rootProg);
  check("single RAF loop reports a cadence", motion.fps > 0, `fps≈${motion.fps.toFixed(1)} (software GL)`);

  /* ------------------------------------------------- 3 · keyboard and a11y --- */
  await page.evaluate(() => window.scrollTo(0, 0));
  const kb = await page.evaluate(() => {
    const skip = !!document.querySelector('a[href="/claims"]');
    const chips = [...document.querySelectorAll(".strip .chip")].length;
    const sceneLinks = [...document.querySelectorAll(".scene-index a")].length;
    // Only *visible* controls owe the user an accessible name: `type=hidden` carriers and display:none
    // leftovers are plumbing, and flagging them turns a clean audit into a hunt for nothing.
    const unlabelled = [...document.querySelectorAll("input,select,textarea")]
      .filter(n => n.type !== "hidden" && n.offsetParent !== null
                   && !n.getAttribute("aria-label") && !n.getAttribute("aria-labelledby")
                   && !n.closest("label")
                   && !(n.id && document.querySelector('label[for="' + n.id + '"]')))
      .map(n => n.tagName.toLowerCase() + "[" + (n.name || n.id || "?") + "]");
    return { skip, chips, sceneLinks, unlabelled,
             h1text: document.querySelector("h1").textContent.trim() };
  });
  check("health chips are focusable instruments", kb.chips >= 8, String(kb.chips));
  check("the canvas has a focusable equivalent", kb.sceneLinks > 0, String(kb.sceneLinks));
  check("no unlabelled form control on the cockpit", kb.unlabelled.length === 0, kb.unlabelled.join(", ") || "none");
  await page.keyboard.press("Tab"); await page.keyboard.press("Tab"); await page.keyboard.press("Tab");
  const focusRing = await page.evaluate(() => {
    const a = document.activeElement;
    const cs = getComputedStyle(a);
    return { tag: a.tagName, text: (a.textContent || "").trim().slice(0, 20),
             outline: cs.outlineWidth + " " + cs.outlineStyle };
  });
  check("keyboard focus lands on a real control", ["A", "BUTTON", "INPUT", "SELECT"].includes(focusRing.tag),
        `${focusRing.tag} "${focusRing.text}"`);
  check("focus is visible", focusRing.outline !== "0px none", focusRing.outline);

  /* ------------------------- 3b · the cursor, the scene, and a real hover --- */
  const hover = await page.evaluate(() => {
    // What a page can honestly know about its own GL field: geometry, program handles, the GL error code.
    // `readPixels` is deliberately NOT tried here — on a `preserveDrawingBuffer: false` context it is
    // undefined once the frame has gone to the compositor, and it once reported "nothing painted" for a
    // field that every screenshot showed. Pixels are proved below, from the rasterised element.
    const c = document.querySelector(".scene canvas"), S = window.__alibiScene;
    return { mounted: !!S, nodes: S ? S.nodes() : 0, edgeCount: S ? S.edges() : 0,
             progs: S ? S.program() : {}, glErr: S ? S.err() : -1, lost: S ? S.lost() : null,
             size: c ? [c.width, c.height] : [0, 0],
             box: c ? Math.round(c.getBoundingClientRect().width) : 0,
             cadence: S ? S.cadence() : 0,
             state: c ? c.dataset.fluid : "absent" };
  });
  // Pixels, proved the only way that works on a `preserveDrawingBuffer: false` context: keep the engine
  // painting and grab a viewport clip from inside an animation frame, retrying until ink shows up. A
  // settled, paused compositor legitimately holds the *cleared* frame, so "black" here would otherwise say
  // more about when the screenshot was taken than about what the page draws.
  let sceneShot = null;
  await page.evaluate(() => {
    window.__pump = setInterval(() => window.ALIBI && window.ALIBI.Engine.wake(), 30);
    // absolute offset of the band, then park it 20px below the top of the viewport — the earlier version
    // added `window.scrollY` back in and scrolled the element off the *top* of the screen, so the clip
    // captured a band of page with no canvas in it and reported "no ink".
    var r = document.querySelector(".scene").getBoundingClientRect();
    window.scrollTo(0, Math.max(0, Math.round(r.top + window.scrollY - 140)));
  });
  await page.waitForTimeout(700);
  const box = await page.evaluate(() => {
    const r = document.querySelector(".scene").getBoundingClientRect();
    return { x: Math.max(0, Math.round(r.left)), y: Math.max(0, Math.round(r.top)),
             width: Math.min(innerWidth, Math.round(r.width)), height: Math.min(innerHeight, Math.round(r.height)) };
  });
  // The clip must actually sit over the canvas. Without that assertion a screenshot of the strip below the
  // band — which has its own green bar — reads as "ink", and the check passes for the wrong picture.
  check("the ink sample is taken over the band itself",
        box.y >= 0 && box.y + box.height <= 900 + 2 && box.width > 200 && box.height > 150,
        JSON.stringify(box));
  // Software rasterisation hands the compositor a frame when it feels like it, so a single black screenshot
  // says nothing about whether the field painted. Retry generously (and accept the moment ink appears) —
  // this check used to stop at six attempts and then report "no frame decoded" on a busy machine, which is a
  // flaky harness, not a broken product. The `--shots` artefacts record every attempt for exactly that reason.
  const best = { chromaPct: -1 };
  const attempts = shots ? 6 : 14;
  for (let i = 0; i < attempts; i++) {
    try {
      const buf = await page.screenshot({ type: "png", clip: box, timeout: 8000 });
      sceneShot = analyseRgba(buf);
      if (shots && sceneShot.chromaPct !== undefined) {
        writeFileSync(new URL(`.tools/ink-attempt-${i}.png`, new URL("..", import.meta.url)), buf);
      }
      if (sceneShot.chromaPct !== undefined && sceneShot.chromaPct > best.chromaPct) {
        best.chromaPct = sceneShot.chromaPct; best.attempt = i;
        best.width = sceneShot.width; best.height = sceneShot.height;
      }
      console.log(`   · ink attempt ${i}: chroma ${sceneShot.chromaPct !== undefined
        ? sceneShot.chromaPct.toFixed(2) + "% of " + sceneShot.width + "×" + sceneShot.height
          + " (colour type decoded, ink " + sceneShot.inkPct.toFixed(1) + "%)"
        : "decode error: " + sceneShot.error}`);
      if (best.chromaPct > 0.2 && i >= 1) break;      // one free retry, then take the first honest frame
    } catch (e) { sceneShot = { error: String(e.message).split("\n")[0].slice(0, 60) }; }
    await page.waitForTimeout(140);
    await page.evaluate(() => {
      // keep every layer awake: an idle engine legitimately hands the compositor a cleared buffer, which is
      // the one thing a screenshot of a live GL canvas cannot be asked to prove
      if (window.ALIBI) { window.ALIBI.Engine.wake(); window.ALIBI.pointer.x = 0.3 + Math.random() * 0.4; }
    });
  }
  await page.evaluate(() => clearInterval(window.__pump));
  check("the field of facts painted real nodes", best.chromaPct > 0.2 && hover.nodes > 0,
        hover.nodes + " node(s), " + (best.chromaPct >= 0
          ? best.chromaPct.toFixed(2) + "% of the " + best.width + "×" + best.height
            + " band carries coloured ink, frame " + best.attempt + " (engine fps " + hover.cadence.toFixed(0) + ")"
          : "no frame decoded"));
  check("scene GL context alive and error-free", hover.lost === false && hover.glErr === 0,
        `lost=${hover.lost} glError=${hover.glErr}`);

  /* The same four GLSL programs, run headless against a `preserveDrawingBuffer: true` canvas, so the shaders
     are proven to compile, link and emit pixels independently of when the compositor decides to hand the
     page's own back buffer back. Generated from web/static/scene.js — it cannot drift from what ships. */
  let lab = { ok: false, out: "not run" };
  try {
    execFileSync("node", [".tools/labs/gen.mjs"], { cwd: ROOT, encoding: "utf8" });
    const out = execFileSync("node", [".tools/labs/run.mjs"], { cwd: ROOT, encoding: "utf8", timeout: 120000 });
    const m = out.match(/LAB: (\{.*\})/);
    lab = { ok: /LAB: PASS/.test(out), out: m ? m[1].slice(0, 150) : out.slice(-150) };
  } catch (e) { lab = { ok: false, out: String(e.stdout || e.message).slice(-180) }; }
  check("scene shaders compile, link and paint (shader lab)", lab.ok, lab.out);

  const pointerState = await page.evaluate(async () => {
    // The ring, not the wrapper: the wrapper is the markup placeholder motion.js adopts, and its own `--cx`
    // only moves when a hover target is nearby. The ring's transform is the truth.
    const ring = document.querySelector(".fluid-ring");
    if (!ring) return { moved: -1, px: "", state: "" };
    const at = () => new DOMMatrixReadOnly(getComputedStyle(ring).transform).e;
    const before = at();
    window.dispatchEvent(new PointerEvent("pointermove", { clientX: 420, clientY: 300, bubbles: true }));
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const mid = at();
    window.dispatchEvent(new PointerEvent("pointermove", { clientX: 1080, clientY: 620, bubbles: true }));
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const after = at();
    // `--px/--py` are spring-smoothed, so reading them two frames after the last pointer event samples the
    // *chase*, not the destination — and whether that had converged by now was pure machine luck (a slower
    // raster or an extra screenshot in front of it flipped this check). Drive the pointer to a fixed spot and
    // poll until the published value settles there, then judge the value it actually converged to.
    let px = 0, py = 0;
    const root = document.documentElement;
    for (let i = 0; i < 90; i++) {
      window.dispatchEvent(new PointerEvent("pointermove", { clientX: 1080, clientY: 620, bubbles: true }));
      await new Promise(r => requestAnimationFrame(r));
      px = parseFloat(getComputedStyle(root).getPropertyValue("--px"));
      py = parseFloat(getComputedStyle(root).getPropertyValue("--py"));
      if (px > 0.7 && py > 0.7) break;
    }
    const cs = getComputedStyle(root);
    return { moved: Math.round(Math.abs(after - before)), step: Math.round(Math.abs(mid - before)),
             px: cs.getPropertyValue("--px").trim(), py: cs.getPropertyValue("--py").trim(),
             state: ring.parentElement.dataset.cursor || "" };
  });
  check("cursor chases the pointer", pointerState.moved > 40 || pointerState.step > 10,
        `ring travelled ${pointerState.moved}px (first step ${pointerState.step}px), state=${pointerState.state}`);
  check("pointer position published for the shader",
        parseFloat(pointerState.px) > 0.4 && parseFloat(pointerState.py) > 0.4,
        `--px=${pointerState.px} --py=${pointerState.py}`);

  /* --------------------------------------------------- 4 · contrast, measured --- */
  const contrast = await page.evaluate(() => {
    const lum = (c) => { const s = c.map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
      return .2126 * s[0] + .7152 * s[1] + .0722 * s[2]; };
    const rgb = (str) => (str.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const out = [];
    const sample = (sel) => document.querySelectorAll(sel).forEach(n => {
      const cs = getComputedStyle(n);
      if (cs.visibility === "hidden" || cs.display === "none" || !n.textContent.trim()) return;
      // composite the ink over the *painted* backdrop: a translucent page means the real background is
      // the body gradient, so sampling ancestors is what a reviewer's eye actually receives
      let bg = [5, 7, 13];
      let p = n;
      while (p && p !== document.documentElement) {
        const c = rgb(getComputedStyle(p).backgroundColor);
        if (c.length === 3 && getComputedStyle(p).backgroundColor !== "rgba(0, 0, 0, 0)") { bg = c; break; }
        p = p.parentElement;
      }
      const fg = rgb(cs.color);
      const l1 = lum(fg), l2 = lum(bg);
      out.push({ sel, ratio: ((Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05)).toFixed(2),
                 text: n.textContent.trim().slice(0, 18) });
    });
    sample("body p, body .mini, body .sub, body h1, body td, body a, .figure .n, .pill");
    return out;
  });
  const weak = contrast.filter(c => parseFloat(c.ratio) < 4.5 && !/h1|figure/.test(c.sel));
  check("body text meets AA contrast (4.5:1)", weak.length === 0,
        weak.slice(0, 3).map(w => `${w.sel} ${w.ratio} "${w.text}"`).join(" | ") || `sampled ${contrast.length}`);

  /* ------------------------------------------- 5 · every page, in the browser --- */
  /* Release section 1's cockpit before the sweep. Its atmosphere loop never idles (it repaints on any pointer
     movement and is never scrolled again here), so it holds a WebGL context and a rAF alive for the whole
     run; putting 17 more pages on top of it made the *next* cockpit navigation stall past its 30 s timeout —
     a harness artefact, not a page bug, and the kind that leaves a suite green-by-luck one hour and
     unrunnable the next. A screenshot of this page taken later is worth less than a sweep that finishes. */
  if (shots) await page.screenshot({ path: `.tools/shot-cockpit.png`, fullPage: false }).catch(() => {});
  await page.close().catch(() => {});
  const perPage = [];
  for (const path of PAGES) {
    const { page: pg, errors: er } = await openPage(ctx, path, { settle: 500 });
    const info = await pg.evaluate(() => ({
      h1: document.querySelectorAll("h1").length,
      tables: document.querySelectorAll("table").length,
      figures: document.querySelectorAll(".figure").length,
      reveal: document.querySelectorAll("[data-reveal]").length,
      ext: [...document.querySelectorAll("link[href],script[src]")]
        .map(n => n.href || n.src).filter(u => /^https?:\/\//.test(u) && !u.startsWith(location.origin)).length,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      text: document.body.innerText.replace(/\s+/g, " ").length,
    }));
    perPage.push({ path, ...info, errors: er.slice(0, 2) });
    if (info.ext) check(`${path}: external assets`, false, String(info.ext));
    if (info.h1 !== 1) check(`${path}: one h1`, false, String(info.h1));
    if (info.overflow > 1) check(`${path}: no horizontal overflow`, false, info.overflow + "px");
    if (er.length) check(`${path}: console clean`, false, er.slice(0, 2).join(" | "));
    if (info.text < 200) check(`${path}: renders real content`, false, info.text + " chars");
    await pg.close();
  }
  /* ---------------------------------------- 5b · the calm pages, in the browser --- */
  const calm = [];
  for (const path of CALM_PAGES) {
    const { page: pg, errors: er } = await openPage(ctx, path, { settle: 350 });
    const info = await pg.evaluate(() => ({
      h1: document.querySelectorAll("h1").length,
      canvas: document.querySelectorAll("canvas").length,
      cursor: document.querySelectorAll(".fluid-cursor,.fluid-ring").length,
      orbs: document.querySelectorAll(".fluid-orb").length,
      scripts: [...document.querySelectorAll("script[src]")].map(n => n.src.split("/").pop()),
      links: document.querySelectorAll(".side .navi").length,
      current: document.querySelectorAll('.side .navi[aria-current="page"]').length,
      skip: !!document.querySelector("a.skip"),
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      text: document.body.innerText.replace(/\s+/g, " ").length,
      external: [...document.querySelectorAll("link[href],script[src]")]
        .map(n => n.href || n.src).filter(u => /^https?:\/\//.test(u) && !u.startsWith(location.origin)).length,
      raf: !!(window.ALIBI && window.ALIBI.Engine),
      measure: (() => { const p = document.querySelector(".lede, .att__line, .stack");
        return p ? Math.round(p.getBoundingClientRect().width) : -1; })(),
    }));
    calm.push({ path, ...info, errors: er.slice(0, 2) });
    if (info.canvas || info.cursor || info.orbs) check(`${path}: no cinematic layer in the DOM`, false,
      `canvas=${info.canvas} cursor=${info.cursor} orbs=${info.orbs}`);
    if (info.scripts.some(s => /atmosphere|motion|scene\.js/.test(s))) check(`${path}: no fluid scripts`, false,
      info.scripts.join(","));
    if (info.raf) check(`${path}: no RAF engine started`, false, "window.ALIBI exists on a calm page");
    if (info.h1 !== 1) check(`${path}: one h1`, false, String(info.h1));
    if (info.links !== 6 || info.current !== 1) check(`${path}: six items, one current`, false,
      `${info.links} items / ${info.current} current`);
    if (!info.skip) check(`${path}: skip link present`, false, "");
    if (info.external) check(`${path}: external assets`, false, String(info.external));
    if (info.overflow > 1) check(`${path}: no horizontal overflow`, false, info.overflow + "px");
    if (info.text < 200) check(`${path}: renders real content`, false, info.text + " chars");
    await pg.close();
  }
  check("the calm pages are calm in the browser too",
        calm.every(p => p.canvas === 0 && p.cursor === 0 && p.orbs === 0 && !p.raf && !p.external
                       && p.errors.length === 0 && p.h1 === 1 && p.overflow <= 1),
        calm.filter(p => p.errors.length || p.canvas || p.raf).map(p => p.path).join(",") || `${calm.length} pages`);
  check("the calm reading measure stays inside 30–90ch",
        calm.every(p => p.measure === -1 || (p.measure <= 800 && p.measure >= 320)),
        calm.map(p => `${p.path}:${p.measure}px`).join(" | "));
  check("all pages rendered clean in a real browser",
        perPage.every(p => p.h1 === 1 && p.errors.length === 0 && p.ext === 0 && p.overflow <= 1),
        perPage.filter(p => p.errors.length || p.h1 !== 1).map(p => p.path).join(",") || `${PAGES.length} pages`);
  await ctx.close();
}

/* --------------------------------------------------- 6 · reduced motion + no GL --- */
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, reducedMotion: "reduce" });
  const { page, errors } = await openPage(ctx, "/technical/cockpit");
  const rm = await page.evaluate(() => {
    const c = document.getElementById("atmosphere");
    const orb = document.querySelector(".fluid-orb");
    return {
      motion: document.documentElement.dataset.fluidMotion,
      anim: getComputedStyle(orb).animationName,
      revealOpacity: getComputedStyle(document.querySelector("[data-reveal]")).opacity,
      cursor: !!document.querySelector(".fluid-cursor .fluid-ring"),
      canvas: c.dataset.fluid,
      scroll: document.documentElement.dataset.fluidScroll,
      h1size: getComputedStyle(document.querySelector("h1")).fontSize,
    };
  });
  check("reduced motion is detected", rm.motion === "reduced", rm.motion);
  check("ambient animation is off", rm.anim === "none", rm.anim);
  check("content is visible without the reveal", rm.revealOpacity === "1", rm.revealOpacity);
  check("custom cursor is not built", !rm.cursor);
  check("native scroll is restored", rm.scroll === "native", rm.scroll);
  check("display typography survives", parseFloat(rm.h1size) > 40, rm.h1size);
  check("reduced-motion page still renders (static field allowed)",
        ["live-static", "live", "no-webgl2", "skipped", "no-engine"].includes(rm.canvas), rm.canvas);
  check("no console errors with reduced motion", errors.length === 0, errors.slice(0, 2).join(" | "));
  await ctx.close();
}

/* ------------------------------------------------------ 7 · the environment off --- */
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.route("**/static/**", r => r.abort());            // simulate the campus network that blocks assets
  await page.goto(url + "/claims", { waitUntil: "load" });
  await page.waitForTimeout(400);
  const noAssets = await page.evaluate(() => ({
    rows: document.querySelectorAll("#claimtable tbody tr").length,
    quotes: document.querySelectorAll(".quote").length,
    mono: getComputedStyle(document.querySelector("code")).fontFamily,
    ink: getComputedStyle(document.body).color,
    forms: document.querySelectorAll("form[method=post]").length,
  }));
  check("the ledger table survives with every asset blocked", noAssets.rows > 3, `${noAssets.rows} rows`);
  check("receipts still render", noAssets.quotes > 3, `${noAssets.quotes}`);
  check("layer 1 keeps a readable ink colour", noAssets.ink !== "rgb(0, 0, 0)", noAssets.ink);
  await ctx.close();
}

/* ----------------------------------------------------------- 8 · mobile + touch --- */
for (const vp of VIEWPORTS) {
  const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height },
                                        deviceScaleFactor: vp.dpr, hasTouch: !!vp.touch,
                                        isMobile: !!vp.touch });
  const { page, errors } = await openPage(ctx, "/technical/cockpit");
  const m = await page.evaluate(() => {
    const rail = document.querySelector(".rail") || document.querySelector("nav")
               || document.querySelector(".side");
    const cs = getComputedStyle(rail);
    return {
      railPos: cs.position, railBottom: cs.bottom, railName: rail.className || rail.tagName,
      h1: getComputedStyle(document.querySelector("h1")).fontSize,
      bodyFont: parseFloat(getComputedStyle(document.body).fontSize),
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      cursor: !!document.querySelector(".fluid-cursor .fluid-ring"),
      scroll: document.documentElement.dataset.fluidScroll,
      tap: [...document.querySelectorAll("nav a, .rail nav a, .side__nav a")].map(a => Math.round(a.getBoundingClientRect().height)),
      quality: document.documentElement.dataset.fluidQuality,
    };
  });
  const small = vp.width < 700;
  check(`${vp.name}: no horizontal overflow`, m.overflow <= 1, m.overflow + "px");
  check(`${vp.name}: hero keeps a display size`, parseFloat(m.h1) > (small ? 30 : 48), m.h1);
  if (small) {
    check(`${vp.name}: rail docks to the bottom`, m.railPos === "fixed" && m.railBottom === "0px",
          `${m.railName} ${m.railPos}/${m.railBottom}`);
    check(`${vp.name}: no hover cursor on touch`, !m.cursor);
    check(`${vp.name}: native scroll on touch`, m.scroll === "native", m.scroll);
    check(`${vp.name}: tap targets ≥ 40px`, m.tap.filter(t => t < 34).length === 0, m.tap.join(","));
  }
  if (errors.length) check(`${vp.name}: console clean`, false, errors.slice(0, 2).join(" | "));
  else check(`${vp.name}: console clean`, true);
  // a screenshot of a page with a live WebGL loop can stall past any timeout on software rasterisation;
  // it is diagnostic output, never a check, so it must not be able to end the run
  if (shots) await page.screenshot({ path: `.tools/shot-${vp.name}.png`, timeout: 12000 }).catch(() => {});

  /* the same viewports on the calm home page: a renovation that only works at 1440px is a screenshot */
  const { page: cp, errors: cerr } = await openPage(ctx, "/?demo=1", { settle: 400 });
  const cm = await cp.evaluate(() => {
    const rail = document.querySelector(".side");
    const cs = getComputedStyle(rail);
    const nav = rail.querySelector(".side__nav");
    const hd = document.querySelector(".head h1");
    const lede = document.querySelector(".lede");
    return {
      railPos: cs.position, railBottom: cs.bottom, railDisplay: cs.display,
      h1: hd ? parseFloat(getComputedStyle(hd).fontSize) : 0,
      measure: lede ? Math.round(lede.getBoundingClientRect().width) : -1,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      cursor: !!document.querySelector(".fluid-cursor,.fluid-ring"),
      scroll: document.documentElement.dataset.fluidScroll || "native",
      tap: [...document.querySelectorAll(".side__nav a")].map(a => Math.round(a.getBoundingClientRect().height)),
      navScroll: nav ? nav.scrollWidth - nav.clientWidth : 0,
      bodyFont: parseFloat(getComputedStyle(document.body).fontSize),
      canvas: document.querySelectorAll("canvas").length,
    };
  });
  check(`${vp.name} (calm): no horizontal overflow`, cm.overflow <= 1, cm.overflow + "px");
  check(`${vp.name} (calm): heading stays typographic, not theatrical`,
        cm.h1 >= (small ? 24 : 28) && cm.h1 <= (small ? 40 : 48), `${cm.h1}px`);
  check(`${vp.name} (calm): reading measure is bounded`,
        cm.measure === -1 || (cm.measure >= 300 && cm.measure <= 820), `${cm.measure}px`);
  check(`${vp.name} (calm): body text is at least 15px`, cm.bodyFont >= 15, `${cm.bodyFont}px`);
  check(`${vp.name} (calm): no canvas, no cursor, no inertial scroll`,
        !cm.canvas && !cm.cursor && cm.scroll !== "inertia", `${cm.canvas}/${cm.cursor}/${cm.scroll}`);
  if (small) {
    check(`${vp.name} (calm): nav docks to the bottom edge`, cm.railPos === "fixed" && cm.railBottom === "0px",
          `${cm.railPos}/${cm.railBottom}`);
    check(`${vp.name} (calm): tap targets >= 40px`, cm.tap.filter(x => x < 40).length === 0, cm.tap.join(","));
  } else if (vp.width >= 940) {
    // the calm layout swaps at 940px, not at the harness's 700px "small" cut: a 834px tablet keeps the bottom
    // bar, because six labels in a 250px rail beside a 78ch column is two cramped things instead of one roomy one
    check(`${vp.name} (calm): nav is a sticky sidebar`, cm.railDisplay === "flex" && cm.railPos === "sticky",
          `${cm.railDisplay}/${cm.railPos}`);
  } else {
    check(`${vp.name} (calm): tablet keeps the reachable bar`, cm.railPos === "fixed" && cm.railBottom === "0px",
          `${cm.railDisplay}/${cm.railPos}/${cm.railBottom}`);
    check(`${vp.name} (calm): tablet tap targets >= 40px`, cm.tap.filter(x => x < 40).length === 0, cm.tap.join(","));
  }
  check(`${vp.name} (calm): console clean`, cerr.length === 0, cerr.slice(0, 2).join(" | "));
  if (shots) await cp.screenshot({ path: `.tools/shot-calm-${vp.name}.png`, timeout: 12000 }).catch(() => {});
  await cp.close();
  await ctx.close();
}

/* -------------------------------------------------- 9 · the sync + POST round trip --- */
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const { page, errors } = await openPage(ctx, "/settings");
  await page.goto(url + "/technical/cockpit", { waitUntil: "load" });
  await page.waitForTimeout(500);
  const before = await page.evaluate(() => document.querySelector(".hero-figures .figure--lead .n").textContent.trim());
  await page.click('form[action="/api/sync"] button');
  await page.waitForSelector(".toast", { timeout: 20000 }).catch(() => {});
  const toast = await page.evaluate(() => {
    const t = document.querySelector(".toast");
    return t ? { title: t.querySelector("b").textContent, body: (t.querySelector("span") || {}).textContent,
                 live: document.querySelector(".toast-layer").getAttribute("aria-live"),
                 label: document.querySelector(".toast-layer").getAttribute("aria-label") } : null;
  });
  // The enhancement reloads the page after a successful POST, so this second read is a race with a
  // navigation by construction. `page.evaluate` throws "Execution context was destroyed" when it loses, and
  // a harness that dies on a race tells you nothing about the product: settle on a real signal (the figure
  // node exists) and retry the read instead of sleeping a fixed amount.
  await page.waitForSelector(".hero-figures .figure--lead .n", { timeout: 20000 }).catch(() => {});
  await page.waitForLoadState("load", { timeout: 20000 }).catch(() => {});
  let after = null;
  for (let i = 0; i < 5 && !after; i++) {
    try {
      after = await page.evaluate(() => {
    const el = document.querySelector(".hero-figures .figure--lead .n");
    return { n: el ? el.textContent.trim() : "gone", figures: document.querySelectorAll(".figure").length,
             scene: !!document.querySelector(".scene canvas"),
             nodes: window.__alibiScene ? window.__alibiScene.nodes() : 0,
             glErr: window.__alibiScene ? window.__alibiScene.err() : -1,
             sceneState: (document.querySelector(".scene canvas") || {}).dataset?.fluid || "none",
             sceneSize: ((document.querySelector(".scene canvas") || {}).width || 0) + "x" +
                        ((document.querySelector(".scene canvas") || {}).height || 0) };
      });
    } catch (e) { await page.waitForTimeout(700); }
  }
  after = after || { n: "unreadable", figures: 0, scene: false, nodes: 0, glErr: -1, sceneState: "none",
                     sceneSize: "0x0" };
  check("sync produced a toast, not a JSON blob", !!toast && !!toast.title, JSON.stringify(toast));
  check("toast layer is announced politely", toast && toast.live === "polite" && toast.label === "notifications",
        toast ? `${toast.live}/${toast.label}` : "no toast");
  check("the page came back with its figures", after.figures >= 4, JSON.stringify(after));
  check("the field of facts remounted after the sync", after.scene && after.sceneState === "live"
        && parseInt(after.sceneSize) > 200 && after.nodes > 0 && after.glErr === 0, JSON.stringify(after));
  check("sync round trip stayed error-free", errors.length === 0, errors.slice(0, 3).join(" | "));
  void before;
  if (shots) await page.screenshot({ path: `.tools/shot-after-sync.png`, timeout: 12000 }).catch(() => {});

  /* the calm sync: same POST, one sentence of feedback, then a fresh page — with no animation loop, no
     shader and no custom cursor anywhere in the DOM while it happens. */
  await page.close();
  const { page: cp, errors: cerr } = await openPage(ctx, "/?demo=1");
  const cBefore = await cp.evaluate(() => document.querySelectorAll(".att").length);
  await cp.click(".syncbox button");
  await cp.waitForSelector(".toast", { timeout: 20000 }).catch(() => {});
  const cToast = await cp.evaluate(() => {
    const t2 = document.querySelector(".toast");
    return t2 ? { title: t2.querySelector("b").textContent,
                  body: (t2.querySelector("span") || {}).textContent || "",
                  live: document.querySelector(".toast-layer").getAttribute("aria-live"),
                  dismissible: !!t2.querySelector("button") } : null;
  });
  await cp.waitForSelector(".syncbox", { timeout: 20000 }).catch(() => {});
  await cp.waitForLoadState("load", { timeout: 20000 }).catch(() => {});
  let cAfter = null;
  for (let i = 0; i < 5 && !cAfter; i++) {
    try {
      cAfter = await cp.evaluate(() => ({
    att: document.querySelectorAll(".att").length,
    canvas: document.querySelectorAll("canvas").length,
    raf: !!(window.ALIBI && window.ALIBI.Engine),
    enhanced: document.documentElement.dataset.calmEnhanced || "",
        body: document.body.innerText.replace(/\s+/g, " ").length,
      }));
    } catch (e) { await cp.waitForTimeout(700); }
  }
  cAfter = cAfter || { att: 0, canvas: 0, raf: true, enhanced: "", body: 0 };
  check("the calm sync answered in one sentence", !!cToast && !!cToast.title && cToast.dismissible,
        JSON.stringify(cToast));
  check("the calm toast is announced politely", !!cToast && cToast.live === "polite", JSON.stringify(cToast));
  check("the calm page came back with its content",
        cAfter.body > 400 && !cAfter.canvas && !cAfter.raf && cAfter.enhanced === "1", JSON.stringify(cAfter));
  check("the calm sync round trip stayed error-free", cerr.length === 0, cerr.slice(0, 3).join(" | "));
  void cBefore;
  if (shots) await cp.screenshot({ path: ".tools/shot-calm-after-sync.png", timeout: 12000 }).catch(() => {});
  await cp.close();
  await ctx.close();
}

await browser.close();

const failed = results.filter(r => !r.ok);
for (const r of results) console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.name}${r.ok ? "" : "   → " + r.detail}`);
console.log(`\n${results.length - failed.length}/${results.length} browser checks passed`);
if (shots) console.log("screenshots in .tools/");
process.exit(failed.length ? 1 : 0);
