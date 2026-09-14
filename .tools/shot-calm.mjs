import { chromium } from "playwright";
const b = await chromium.launch({ args: ["--no-sandbox"] });
const VPS = [["phone-390", 390, 844, 3, true], ["tablet-834", 834, 1112, 2, true], ["desktop-1440", 1440, 900, 1, false]];
for (const [name, w, h, dpr, touch] of VPS) {
  const ctx = await b.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: dpr, hasTouch: touch, isMobile: touch });
  for (const path of ["/?demo=1", "/tasks", "/review", "/calendar?view=month", "/evidence"]) {
    const p = await ctx.newPage();
    const errs = [];
    p.on("console", m => m.type() === "error" && errs.push(m.text()));
    await p.goto("http://127.0.0.1:8000" + path, { waitUntil: "load" });
    await p.waitForTimeout(450);
    const tag = path.replace(/[^a-z]/gi, "").slice(0, 12) || "home";
    await p.screenshot({ path: `.tools/calm-${name}-${tag}.png`, fullPage: false });
    const geo = await p.evaluate(() => {
      const nav = document.querySelector(".side__nav");
      return { overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
               navRows: Math.round(nav.getBoundingClientRect().height),
               navScrollW: nav.scrollWidth - nav.clientWidth,
               errs: [] };
    });
    console.log(`${name} ${path} nav=${geo.navRows}px slack=${geo.navScrollW}px pageOverflow=${geo.overflow}px js=${errs.length}`);
    await p.close();
  }
  await ctx.close();
}
await b.close();
