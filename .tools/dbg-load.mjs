import { chromium } from "playwright";
const b = await chromium.launch({ args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--disable-gpu-sandbox", "--no-sandbox"] });
for (const ctx of [0, 1, 2]) {
  const c = await b.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await c.newPage();
  const t0 = Date.now();
  try { await p.goto("http://127.0.0.1:8000/technical/cockpit", { waitUntil: "load", timeout: 60000 }); }
  catch (e) { console.log(`ctx${ctx}: FAILED after ${Date.now()-t0}ms — ${e.message.split("\n")[0]}`); }
  const t1 = Date.now();
  const info = await p.evaluate(() => ({ canvas: document.querySelectorAll("canvas").length,
      paint: document.getAnimations ? document.getAnimations().length : -1,
      ready: document.readyState,
      perf: (() => { const n = performance.getEntriesByType("navigation")[0];
        return { domContentLoaded: Math.round(n.domContentLoadedEventEnd - n.startTime), load: Math.round(n.loadEventEnd - n.startTime),
                 ttfb: Math.round(n.responseStart - n.requestStart) }; })() }));
  console.log(`ctx${ctx}: goto=${t1-t0}ms`, JSON.stringify(info));
  await c.close();
}
await b.close();
