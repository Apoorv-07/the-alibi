import { chromium } from "playwright";
const b = await chromium.launch({ args: ["--no-sandbox"] });
const ctx = await b.newContext({ viewport: { width: 834, height: 900 } });
const p = await ctx.newPage();
await p.goto("http://127.0.0.1:8000/tasks", { waitUntil: "load" });
const r = await p.evaluate(() => {
  const out = { sideRules: [], media: [], errors: [] };
  for (const sheet of document.styleSheets) {
    const href = sheet.href || "inline";
    let rules;
    try { rules = sheet.cssRules; } catch (e) { out.errors.push(href + ": " + e.name); continue; }
    for (let i = 0; i < rules.length; i++) {
      const ru = rules[i];
      if (ru.type === 4 /* media */) {
        out.media.push(`${href}@${i}: ${ru.conditionText} rules=${ru.cssRules.length}`);
        for (const inner of ru.cssRules) if (/\.side\b/.test(inner.selectorText || "")) out.sideRules.push(`IN ${ru.conditionText}: ${inner.style.display || "?"}`);
      } else if (/\.side\b/.test(ru.selectorText || "")) {
        out.sideRules.push(`${href}@${i} {${ru.selectorText}} display=${ru.style.display} pos=${ru.style.position}`);
      }
    }
  }
  return out;
});
console.log(JSON.stringify(r, null, 1).slice(0, 2600));
await b.close();
