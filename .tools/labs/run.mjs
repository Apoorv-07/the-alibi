import { chromium } from "playwright";
const b = await chromium.launch({ args: ["--use-angle=swiftshader","--enable-unsafe-swiftshader","--no-sandbox","--disable-gpu-sandbox"] });
const p = await (await b.newContext()).newPage();
p.on("console", m => console.log("[" + m.type() + "]", m.text().slice(0, 220)));
await p.goto("file://" + new URL("scene-lab.html", import.meta.url).pathname);
await p.waitForTimeout(500);
const r = await p.evaluate(() => window.LAB);
console.log("LAB:", JSON.stringify(r));
const ok = r && r.litPct > 0.05 && Object.values(r.compile).every(v => v === "ok")
        && Object.values(r.link).every(v => v === "ok");
console.log(ok ? "LAB: PASS" : "LAB: FAIL");
await p.screenshot({ path: new URL("scene-lab.png", import.meta.url).pathname });
await b.close();
process.exit(ok ? 0 : 1);
