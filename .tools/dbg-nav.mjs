import { chromium } from "playwright";
const b = await chromium.launch({ args: ["--no-sandbox"] });
for (const w of [834, 1200]) {
  const ctx = await b.newContext({ viewport: { width: w, height: 900 } });
  const p = await ctx.newPage();
  await p.goto("http://127.0.0.1:8000/tasks", { waitUntil: "load" });
  const r = await p.evaluate(() => {
    const s = document.querySelector(".side");
    const cs = getComputedStyle(s);
    return { display: cs.display, pos: cs.position, hasCalmAttr: document.documentElement.hasAttribute("data-calm"),
             sheets: [...document.styleSheets].map(x => (x.href || "inline") + ":" + (x.cssRules.length)),
             inner: s.innerHTML.slice(0, 60) };
  });
  console.log(w, JSON.stringify(r));
  await ctx.close();
}
await b.close();
