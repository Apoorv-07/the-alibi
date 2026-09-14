<!doctype html><meta charset=utf-8><title>scene lab</title>
<body style="margin:0;background:#05070d">
<canvas id="c" width="640" height="420" style="display:block"></canvas>
<script>
/* Extracted from web/static/scene.js by .tools/labs/gen.py — same GLSL, a `preserveDrawingBuffer: true`
   context, and a synthetic 18-node ring. The page itself is verified separately by geometry (a canvas
   measured at its CSS box, a mounted live context, `data-fluid="live"`); this lab exists to prove the
   SHADERS paint, which a compositor-owned back buffer cannot be read for inside a test. */
const S = __SCENE_SHADERS__;
const gl = document.getElementById('c').getContext('webgl2',
  { alpha: true, antialias: false, depth: false, premultipliedAlpha: false, preserveDrawingBuffer: true });
const report = { compile: {}, link: {}, err: null, litPct: null, nodes: 0 };
function sh(t, s, tag) {
  const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o);
  report.compile[tag] = gl.getShaderParameter(o, gl.COMPILE_STATUS) ? "ok" : gl.getShaderInfoLog(o);
  return o;
}
function link(v, f, tag, names, out) {
  const a = sh(gl.VERTEX_SHADER, v, tag + ".vs"), b = sh(gl.FRAGMENT_SHADER, f, tag + ".fs");
  const p = gl.createProgram(); gl.attachShader(p, a); gl.attachShader(p, b); gl.linkProgram(p);
  report.link[tag] = gl.getProgramParameter(p, gl.LINK_STATUS) ? "ok" : gl.getProgramInfoLog(p);
  names.forEach(n => { out[n] = gl.getUniformLocation(p, n); });
  return p;
}
function persp(o, fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
  o[0] = f / aspect; o[5] = f; o[10] = (far + near) * nf; o[11] = -1; o[14] = 2 * far * near * nf;
  return o;
}
const proj = persp(new Float32Array(16), 0.85, 640 / 420, 0.1, 30);
const view = new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,-5.4,1]);
const model = new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]);
const nloc = {}, lloc = {};
const nProg = link(S.NODE_VS, S.NODE_FS, "node",
  ["uProj","uView","uModel","uPx","uGround","uReview","uConflict"], nloc);
const lProg = link(S.LINE_VS, S.LINE_FS, "line", ["uProj","uView","uModel","uTint"], lloc);
if (nProg && lProg) {
  const N = 18, node = new Float32Array(N * 8);
  for (let i = 0; i < N; i++) {
    const a = i / N * Math.PI * 2, r = 1.1 + (i % 3) * 0.09;
    node[i*8+0] = Math.cos(a) * r; node[i*8+1] = (i % 5) / 4 * 1.5 - 0.75; node[i*8+2] = Math.sin(a) * r;
    node[i*8+3] = 0.052; node[i*8+4] = 0; node[i*8+5] = 0;
    node[i*8+6] = (i % 3) * 0.4; node[i*8+7] = i % 3;
  }
  const quad = new Float32Array([-1,-1, 1,-1, -1,1, 1,1]);
  const nb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, nb);
  gl.bufferData(gl.ARRAY_BUFFER, node, gl.STATIC_DRAW);
  const qb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, qb);
  gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);
  const vao = gl.createVertexArray(); gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, nb);
  gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 32, 0);
  gl.vertexAttribDivisor(0, 1);
  gl.enableVertexAttribArray(2); gl.vertexAttribPointer(2, 2, gl.FLOAT, false, 32, 24);
  gl.vertexAttribDivisor(2, 1);
  gl.bindBuffer(gl.ARRAY_BUFFER, qb);
  gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 8, 0);
  gl.vertexAttribDivisor(1, 0);
  gl.viewport(0, 0, 640, 420); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
  gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.useProgram(nProg);
  gl.uniformMatrix4fv(nloc.uProj, false, proj); gl.uniformMatrix4fv(nloc.uView, false, view);
  gl.uniformMatrix4fv(nloc.uModel, false, model);
  // Set exactly the way scene.js sets them: uPx is a float (half the canvas height, so the node size in aData.w
  // reads as pixels at unit depth) and the three state colours are vec3s. Passing floats to the vec3s was a lab
  // bug that reported "nothing painted" for a shader that was fine — which is the whole reason the lab exists.
  gl.uniform1f(nloc.uPx, 420 * 0.5);
  gl.uniform3f(nloc.uGround, .36, .85, .60);
  gl.uniform3f(nloc.uReview, 1.00, .74, .32);
  gl.uniform3f(nloc.uConflict, 1.00, .42, .42);
  gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, N);
  report.nodes = N;
  const buf = new Uint8Array(640 * 420 * 4);
  gl.readPixels(0, 0, 640, 420, gl.RGBA, gl.UNSIGNED_BYTE, buf);
  let lit = 0;
  for (let i = 3; i < buf.length; i += 4) if (buf[i] > 6) lit++;
  report.litPct = lit / (640 * 420) * 100;
  report.err = gl.getError();
}
if (nProg) {
  const nu = gl.getProgramParameter(nProg, gl.ACTIVE_UNIFORMS);
  report.uniforms = [];
  for (let i = 0; i < nu; i++) { const u = gl.getActiveUniform(nProg, i);
    report.uniforms.push(u.name + " size=" + u.size + " type=" + u.type + " loc=" + gl.getUniformLocation(nProg, u.name)); }
  const na = gl.getProgramParameter(nProg, gl.ACTIVE_ATTRIBUTES);
  report.attribs = [];
  for (let i = 0; i < na; i++) { const a = gl.getActiveAttrib(nProg, i);
    report.attribs.push(a.name + " size=" + a.size + " type=" + a.type); }
}
window.LAB = report;
document.title = JSON.stringify(report);
</script></body>
