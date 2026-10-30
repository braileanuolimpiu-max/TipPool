/* TipPool's front page: the receipt prints line by line as it comes out of the slot at the bottom of the screen,
   and smoke drifts through the light behind it. People who asked for less motion get the whole receipt, still. */
(() => {
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const slot = document.getElementById("rc-slot");
  const SLOT_H = 34;

  /* ---------- printing: everything above the slot is printed, one line at a time ---------- */
  const items = [...document.querySelectorAll(".rc-p, .rc-stamp, .rc-note")];
  const queue = [];
  let next = 0, printing = false;
  const print = () => {
    const el = queue.shift();
    if (!el) { printing = false; slot.classList.remove("busy"); return; }
    el.classList.add("on");
    setTimeout(print, el.classList.contains("rc-stamp") ? 320 : 65);
  };
  const feed = () => {
    const edge = window.innerHeight - SLOT_H;
    while (next < items.length && items[next].getBoundingClientRect().top < edge) queue.push(items[next++]);
    if (queue.length && !printing) { printing = true; slot.classList.add("busy"); print(); }
    // the end of the receipt has come out: the printer slides away
    const stub = document.querySelector(".rc-stub");
    slot.classList.toggle("gone", stub.getBoundingClientRect().bottom < edge);
  };
  if (still || !("requestAnimationFrame" in window)) items.forEach((el) => el.classList.add("on"));
  let queued = false;
  const onScroll = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; feed(); }); } };
  window.addEventListener("scroll", onScroll, {passive: true});
  window.addEventListener("resize", onScroll);
  feed();

  /* ---------- the room: smoke drifting through a warm lamp, with a cold light low on the right ---------- */
  const canvas = document.getElementById("rc-smoke");
  let gl = null;
  try { gl = canvas.getContext("webgl", {antialias: false, depth: false, alpha: false, powerPreference: "low-power"}); } catch (e) { gl = null; }
  if (!gl) return;  // the CSS gradient stays
  const VERT = "attribute vec2 a; void main(){ gl_Position = vec4(a, 0.0, 1.0); }";
  const FRAG = `
    precision mediump float;
    uniform vec2 r; uniform float t; uniform vec2 m; uniform float s;
    float h(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
    float n(vec2 p){ vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
      return mix(mix(h(i), h(i + vec2(1, 0)), f.x), mix(h(i + vec2(0, 1)), h(i + vec2(1, 1)), f.x), f.y); }
    float fbm(vec2 p){ float v = 0.0, a = 0.5; for (int i = 0; i < 5; i++) { v += a * n(p); p = p * 2.03 + 7.1; a *= 0.5; } return v; }
    void main(){
      vec2 p = (gl_FragCoord.xy - 0.5 * r) / r.y;
      float q = fbm(p * 1.5 + vec2(0.0, -t * 0.035));
      float k = fbm(p * 2.1 + q * 1.9 + vec2(t * 0.03, -t * 0.05));
      vec2 lampAt = vec2(m.x * 0.12, 0.66 - s * 0.25);
      float lamp = smoothstep(1.15, 0.0, length((p - lampAt) * vec2(0.9, 1.35)));
      float cool = smoothstep(1.3, 0.0, length(p - vec2(0.95, -0.7 + s * 0.3)));
      vec3 c = vec3(0.027, 0.024, 0.039);
      c += vec3(1.0, 0.6, 0.22) * lamp * (0.10 + 0.62 * k * k);
      c += vec3(0.22, 0.45, 1.0) * cool * (0.05 + 0.34 * k * k) * (0.7 + s * 0.6);
      c += vec3(0.95, 0.85, 0.7) * pow(k, 4.0) * 0.08;
      gl_FragColor = vec4(c, 1.0);
    }`;
  const shader = (type, src) => { const sh = gl.createShader(type); gl.shaderSource(sh, src); gl.compileShader(sh); return sh; };
  const prog = gl.createProgram();
  gl.attachShader(prog, shader(gl.VERTEX_SHADER, VERT));
  gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FRAG));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return;
  gl.useProgram(prog);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const a = gl.getAttribLocation(prog, "a");
  gl.enableVertexAttribArray(a); gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0);
  const u = (name) => gl.getUniformLocation(prog, name);
  const uR = u("r"), uT = u("t"), uM = u("m"), uS = u("s");

  // smoke is soft anyway: draw it at a fraction of the screen's pixels and let the browser scale it up
  const SCALE = 0.4;
  const size = () => {
    canvas.width = Math.max(1, Math.round(window.innerWidth * SCALE));
    canvas.height = Math.max(1, Math.round(window.innerHeight * SCALE));
    gl.viewport(0, 0, canvas.width, canvas.height);
  };
  size();
  const mouse = {x: 0, y: 0, tx: 0, ty: 0};
  window.addEventListener("pointermove", (e) => { mouse.tx = e.clientX / window.innerWidth * 2 - 1; mouse.ty = e.clientY / window.innerHeight * 2 - 1; }, {passive: true});
  const scrolled = () => Math.min(1, window.scrollY / Math.max(1, document.documentElement.scrollHeight - window.innerHeight));
  const draw = (now) => {
    mouse.x += (mouse.tx - mouse.x) * 0.03; mouse.y += (mouse.ty - mouse.y) * 0.03;
    gl.uniform2f(uR, canvas.width, canvas.height);
    gl.uniform1f(uT, still ? 12 : now / 1000);
    gl.uniform2f(uM, mouse.x, mouse.y);
    gl.uniform1f(uS, scrolled());
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  };
  canvas.classList.add("on");
  if (still) {
    draw(0);
    window.addEventListener("resize", () => { size(); draw(0); });
    return;
  }
  window.addEventListener("resize", size);
  const loop = (now) => { if (!document.hidden) draw(now); requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
})();
