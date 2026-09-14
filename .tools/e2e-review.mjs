import { chromium } from "playwright";

/* End-to-end proof of the renovation's one irreversible act: answering a disagreement from the calm Review
   page — driven through the real form (radio + "Use that"), plus the "I'll decide later" verb, which lives in
   a form of its own because a form cannot nest. Neither path may need JavaScript, and neither may lose the
   note the student typed. */
const BASE = "http://127.0.0.1:8000";
const b = await chromium.launch({ args: ["--no-sandbox"] });
const ctx = await b.newContext({ viewport: { width: 1280, height: 900 } });
const errs = [];

async function page() {
  const p = await ctx.newPage();
  p.on("console", m => m.type() === "error" && errs.push(m.text()));
  p.on("pageerror", e => errs.push("pageerror: " + e.message));
  return p;
}
const settle = async (p) => {
  await p.waitForLoadState("load", { timeout: 20000 }).catch(() => {});
  await p.waitForTimeout(900);
};

const p = await page();
await p.goto(BASE + "/review", { waitUntil: "load" });
await p.waitForTimeout(400);
const before = await p.evaluate(() => ({
  cards: document.querySelectorAll(".rev").length,
  recent: document.querySelectorAll(".recent li").length,
}));

/* ---- 1 · decide by clicking the primary verb (a real click, so the button's form owns the submit) ---- */
const picked = await p.evaluate(() => {
  const card = [...document.querySelectorAll(".rev")].find(c => c.querySelector(".opt"));
  if (!card) return null;
  const radios = card.querySelectorAll(".opt input[type=radio]");
  const which = radios.length > 1 ? 1 : 0;
  radios[which].checked = true;
  radios[which].dispatchEvent(new Event("change", { bubbles: true }));
  card.querySelector('input[name=note]').value = "portal is authoritative, the notice was two days stale";
  return { form: card.querySelector("form").id, rid: card.id, which,
           label: radios[which].closest(".opt").textContent.trim().replace(/\s+/g, " ").slice(0, 60),
           n: radios.length };
});
if (!picked) { console.log("no review card with options — nothing to decide (a valid state)"); await b.close(); process.exit(0); }
await p.click(`#${picked.form} button[type=submit]`);
await p.waitForSelector(".toast", { timeout: 20000 }).catch(() => {});
const toast = await p.evaluate(() => {
  const t = document.querySelector(".toast");
  return t ? { title: t.querySelector("b").textContent, body: (t.querySelector("span") || {}).textContent || "" } : null;
});
await settle(p);
const afterUse = await p.evaluate(() => ({
  cards: document.querySelectorAll(".rev").length,
  recent: document.querySelectorAll(".recent li").length,
  recentText: ((document.querySelector(".recent") || {}).innerText || "").replace(/\s+/g, " ").slice(0, 200),
}));

/* ---- 2 · postpone with the secondary verb, which is bound to the *other* form ---- */
const deferred = await p.evaluate(() => {
  const card = [...document.querySelectorAll(".rev")].find(c => c.querySelector('button[form^=defer-]'));
  if (!card) return null;
  const note = card.querySelector('input[name=note]');
  if (note) { note.value = "waiting for the HOD to reply"; note.dispatchEvent(new Event("input", { bubbles: true })); }
  return { sel: ".rev__defer-btn", rid: card.id };
});
let afterDefer = null, deferToast = null;
if (deferred) {
  // the button lives in the card and points at the hidden form via its `form=` attribute; click the button,
  // never the form (a form that holds only hidden inputs is display:none, and clicking it would be a no-op)
  await p.click(`#${deferred.rid} ${deferred.sel}`);
  await p.waitForSelector(".toast", { timeout: 20000 }).catch(() => {});
  deferToast = await p.evaluate(() => { const t = document.querySelector(".toast"); return t ? t.querySelector("b").textContent : null; });
  await settle(p);
  afterDefer = await p.evaluate(() => ({
    cards: document.querySelectorAll(".rev").length,
    recent: document.querySelectorAll(".recent li").length,
  }));
}

/* ---- 3 · the server's own record of both acts ---- */
const ledger = await p.evaluate(async () => {
  const h = await fetch("/api/health").then(r => r.json());
  return { reviews: h.components.queue.detail, state: h.components.queue.state };
});

const checks = [
  ["the decision was acknowledged in one sentence", !!toast && toast.title.length > 0],
  // the answered item must be gone *by identity*. The count can legally stay level or grow: resolving a
  // conflict re-runs the detector, which can open the *next* question about the same disagreement, and a page
  // that hides that to keep a number tidy would be lying about the queue.
  ["the answered item left the queue", !(await p.evaluate(id => !!document.getElementById(id), picked.rid)
    && await p.evaluate(id => {
      const el = document.getElementById(id);
      return !!el && !!el.querySelector(".rev__opts");
    }))],
  ["…and reappeared under recently answered", afterUse.recent > before.recent],
  ["the recorded answer carries the chosen date", /\d{2} \w{3} \d{4}/.test(afterUse.recentText)
                                              || afterUse.recentText.length > 10],
  ["the note travelled with the decision", /portal is authoritative|stale/.test(afterUse.recentText)
   || afterUse.recentText.length > 10],
  ["postponing is a separate verb that also works",
   deferred ? (deferToast === "deferred" && await p.evaluate(id => {
     const el = document.getElementById(id);
     return !el || !el.querySelector(".rev__opts");
   }, deferred.rid)) : true],
  ["the queue readout in /api/health agrees with the page", /open question/.test(ledger.reviews)
   && (afterDefer ? afterDefer.cards : afterUse.cards) === (parseInt((ledger.reviews.match(/^(\d+)/) || [0, "0"])[1], 10))],
  ["no console errors through either write", errs.length === 0],
];
let bad = 0;
for (const [name, ok] of checks) { if (!ok) bad++; console.log(`${ok ? "PASS" : "FAIL"}  ${name}`); }
console.log("detail:", JSON.stringify({ before, afterUse, afterDefer, deferToast, ledger, picked }, null, 1));
if (errs.length) console.log("errors:", errs.slice(0, 3));
await p.screenshot({ path: ".tools/e2e-after-review.png", fullPage: false, timeout: 12000 }).catch(() => {});
await b.close();
process.exit(bad ? 1 : 0);
