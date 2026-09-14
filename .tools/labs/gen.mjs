// Regenerate .tools/labs/scene-lab.html from the shaders that actually ship in web/static/scene.js.
// Run after touching any shader source: `node .tools/labs/gen.mjs && node .tools/labs/run.mjs`.
import { readFileSync, writeFileSync } from "node:fs";
const src = readFileSync(new URL("../../web/static/scene.js", import.meta.url), "utf8");
const names = ["NODE_VS", "NODE_FS", "LINE_VS", "LINE_FS"];
const parts = {};
for (const n of names) {
  const m = src.match(new RegExp("var " + n + " = `(.*?)`;", "s"));
  if (!m) { console.error("scene-lab: shader " + n + " not found in scene.js — the file changed shape"); process.exit(2); }
  parts[n] = m[1];
}
const tpl = readFileSync(new URL("scene-lab.tpl", import.meta.url), "utf8");
writeFileSync(new URL("scene-lab.html", import.meta.url), tpl.replace("__SCENE_SHADERS__", JSON.stringify(parts)));
console.log("scene-lab.html regenerated from scene.js (" + names.map(n => n + ":" + parts[n].length).join(" ") + ")");
