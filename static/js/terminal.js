/* TipPool's front page: a night-shift point-of-sale terminal on a bar counter, in the dark.
   Its CRT boots TIPPOOL OS (a 2D canvas, drawn through a CRT shader), the menu is the way in, and the
   receipt printer prints tonight's tip-out. Without WebGL the same screen is shown flat; with reduced
   motion everything is shown finished and still. */
const page = document.querySelector("[data-three]");
const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const flatHost = document.getElementById("tp-flat");
const sceneHost = document.getElementById("tp-scene");
const demoForm = document.getElementById("tp-demo-form");
const LINKS = {login: page.dataset.login, signup: page.dataset.signup};

/* ---------------- TIPPOOL OS: the screen, drawn on a canvas ---------------- */
const W = 1024, H = 768;
const screen = document.createElement("canvas");
screen.width = W; screen.height = H;
const g = screen.getContext("2d");
const AMBER = "#ffb44a", HOT = "#ffd84a", DIM = "#b47c38", BG = "#0d0904";
const MENU = [
  {key: "1", label: "TRY THE DEMO", note: "NO SIGN-UP · SANDBOX MONEY", go: "demo"},
  {key: "2", label: "LOG IN", note: "", go: "login"},
  {key: "3", label: "SIGN UP", note: "FREE", go: "signup"},
  {key: "4", label: "HOW IT WORKS", note: "", go: "how"},
  {key: "5", label: "THE AGENT", note: "PAYPAL + AI", go: "agent"},
];
const BOOT = [
  "TIPPOOL BIOS v1.04   (C) 2026 TIPPOOL SYSTEMS",
  "",
  "MEMORY TEST ............ 640K OK",
  "RECEIPT PRINTER ........ READY",
  "PAYPAL LINK ............ ONLINE",
  "AI CO-PROCESSOR ........ ONLINE",
  "",
  "LOADING TIPPOOL OS",
];
const HOW = [
  ["1", "GUESTS TIP BY QR", "PAYPAL OR VENMO, IN THEIR LANGUAGE."],
  ["2", "AI SPLITS AND EXPLAINS", "YOUR RULES IN PLAIN WORDS. EXACT CENTS."],
  ["3", "ONE PAYOUT PAYS EVERYONE", "ONE PAYPAL BATCH, CHECKED FIRST, NEVER TWICE."],
];
const AGENT_ASK = "CLOSE OUT TONIGHT, ADD $40 CASH AND PAY EVERYONE";
const AGENT_PLAN = ["1  CLOSE OUT THE FRIDAY SHIFT AND SPLIT IT", "2  ADD $40.00 CASH, SHARED BY THE TEAM",
                    "3  PAY THE FRIDAY SHIFT THROUGH PAYPAL"];
const os = {mode: "boot", t0: performance.now(), sel: 0, dirty: true, typed: 0, busy: null, rows: []};

function setMode(mode) {
  os.mode = mode; os.t0 = performance.now(); os.typed = 0; os.dirty = true;
  if (mode === "how") printer.start();
}
function text(s, x, y, size = 34, color = AMBER, align = "left") {
  g.font = `${size}px "VT323", "IBM Plex Mono", monospace`;
  g.fillStyle = color; g.textAlign = align; g.textBaseline = "top";
  g.fillText(s, x, y);
}
function drawScreen(now) {
  const t = (now - os.t0) / 1000;
  g.fillStyle = BG; g.fillRect(0, 0, W, H);
  const blink = still || Math.floor(now / 500) % 2 === 0;
  os.rows = [];
  if (os.mode === "boot") {
    const shown = still ? BOOT.length : Math.min(BOOT.length, Math.floor(t / 0.28));
    BOOT.slice(0, shown).forEach((line, i) => text(line, 64, 64 + i * 44, 34, i === 0 ? HOT : AMBER));
    if (shown === BOOT.length) {
      const p = still ? 1 : Math.min(1, (t - BOOT.length * 0.28) / 1.1);
      g.strokeStyle = AMBER; g.lineWidth = 3; g.strokeRect(64, 470, 600, 40);
      g.fillStyle = AMBER;
      for (let i = 0; i < Math.floor(p * 20); i++) g.fillRect(72 + i * 29.6, 478, 24, 24);
      if (p >= 1) setMode("menu");
    } else if (blink) g.fillRect(64, 64 + shown * 44, 18, 34);
    return;
  }
  // a header on every page
  text("TIPPOOL OS", 64, 44, 72, HOT);
  text("v1.0", 420, 78, 30, DIM);
  text("LUNA BISTRO · TONIGHT  $266.50", W - 64, 58, 30, DIM, "right");
  g.fillStyle = DIM; g.fillRect(64, 132, W - 128, 3);
  if (os.mode === "menu") {
    text("TIP-OUTS, DONE BEFORE THE LIGHTS GO OFF.", 64, 158, 38, AMBER);
    MENU.forEach((m, i) => {
      const y = 236 + i * 76;
      os.rows.push({y0: y - 10, y1: y + 60, i});
      const on = i === os.sel;
      if (on) { g.fillStyle = AMBER; g.fillRect(52, y - 8, W - 104, 62); }
      const c = on ? BG : AMBER;
      text(`[${m.key}]`, 72, y, 46, on ? BG : HOT);
      text(m.label, 178, y, 46, c);
      if (m.note) text(m.note, W - 76, y + 12, 28, on ? BG : DIM, "right");
    });
    text(`USE 1-5, ARROWS + ENTER, OR TAP${blink ? " _" : "  "}`, 64, H - 70, 30, DIM);
  } else if (os.mode === "how") {
    text("HOW IT WORKS", 64, 158, 46, HOT);
    const step = still ? 9 : Math.floor(t / 0.55);
    HOW.forEach(([n, a, b], i) => {
      if (step <= i * 2) return;
      const y = 236 + i * 118;
      text(n, 72, y, 64, HOT);
      text(a, 140, y + 4, 42, AMBER);
      if (step > i * 2 + 1) text(b, 140, y + 50, 30, DIM);
    });
    if (step > 6) text(`PRINTING TONIGHT'S TIP-OUT${".".repeat(1 + Math.floor(now / 400) % 3)}`, 64, 600, 32, AMBER);
    text("[0] BACK", 64, H - 70, 30, DIM);
    os.rows.push({y0: H - 80, y1: H - 30, i: -1});
  } else if (os.mode === "agent") {
    text("THE AGENT · ASK ABOUT YOUR TIPS", 64, 158, 40, HOT);
    const chars = still ? AGENT_ASK.length : Math.min(AGENT_ASK.length, Math.floor(t * 26));
    text("> " + AGENT_ASK.slice(0, chars) + (chars < AGENT_ASK.length && blink ? "_" : ""), 64, 226, 34, AMBER);
    const after = still ? 99 : t - AGENT_ASK.length / 26;
    if (after > 0.6) text("3 STEPS, IN THIS ORDER:", 64, 290, 30, DIM);
    AGENT_PLAN.forEach((line, i) => { if (after > 1 + i * 0.5) text(line, 84, 336 + i * 44, 34, AMBER); });
    if (after > 2.8) text(`CONFIRM? [Y]${blink || after > 4 ? "" : " _"}`, 64, 486, 36, HOT);
    if (after > 4) text("✓ PAID 5 PEOPLE · $306.50 · PAYPAL PAYOUTS", 64, 536, 34, "#9cf0b6");
    if (after > 5) {
      text("THE AI CAN STOP A PAYOUT, NEVER START ONE.", 64, 600, 28, DIM);
      text("AMOUNTS ARE CALCULATED IN CODE, NEVER BY THE AI.", 64, 634, 28, DIM);
    }
    text("[0] BACK", 64, H - 70, 30, DIM);
    os.rows.push({y0: H - 80, y1: H - 30, i: -1});
  } else if (os.mode === "go") {
    text(`${os.busy.label}${".".repeat(1 + Math.floor(now / 300) % 3)}`, 64, 300, 64, HOT);
    text("CONNECTING TO TIPPOOL", 64, 390, 34, AMBER);
  }
}

/* ---------------- choosing from the menu ---------------- */
let flyTo = null;  // set by the 3D scene: flies the camera into the screen, then calls back
function choose(i) {
  if (os.mode === "go") return;
  if (i === -1) { setMode("menu"); return; }
  const m = MENU[i];
  os.sel = i;
  if (m.go === "how" || m.go === "agent") { setMode(m.go); return; }
  os.busy = m; setMode("go");
  const leave = () => {
    if (m.go === "demo") demoForm.requestSubmit ? demoForm.requestSubmit() : demoForm.submit();
    else window.location.href = LINKS[m.go];
  };
  if (flyTo && !still) flyTo(leave); else setTimeout(leave, still ? 0 : 500);
}
document.addEventListener("keydown", (e) => {
  if (e.target.closest && e.target.closest("input, textarea, select, a, button") && e.key !== "Escape") return;
  if (os.mode === "boot") { if (e.key === "Enter" || e.key === " " || e.key === "Escape") setMode("menu"); return; }
  if (/^[1-5]$/.test(e.key)) { e.preventDefault(); choose(Number(e.key) - 1); }
  else if (e.key === "0" || e.key === "Escape" || e.key === "Backspace") { if (os.mode !== "menu") setMode("menu"); }
  else if (os.mode === "menu" && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault(); os.sel = (os.sel + (e.key === "ArrowDown" ? 1 : MENU.length - 1)) % MENU.length; os.dirty = true;
  } else if (os.mode === "menu" && e.key === "Enter") { e.preventDefault(); choose(os.sel); }
});
// a point on the screen (0..1 across and down) chooses the row under it
function pickAt(u, v, click) {
  if (os.mode === "boot") { if (click) setMode("menu"); return false; }
  const y = v * H, hit = os.rows.find((r) => y >= r.y0 && y <= r.y1);
  if (!hit) return false;
  if (hit.i >= 0 && hit.i !== os.sel && os.mode === "menu") { os.sel = hit.i; os.dirty = true; }
  if (click) choose(hit.i);
  return true;
}

/* ---------------- the receipt printer's paper ---------------- */
const PAPER_W = 384, PAPER_H = 1180;
const paperCanvas = document.createElement("canvas");
paperCanvas.width = PAPER_W; paperCanvas.height = PAPER_H;
const RECEIPT = [
  ["c", "LUNA BISTRO", 30], ["c", "TIP-OUT · TONIGHT", 22], ["-"],
  ["l", "GUEST TIPS (PAYPAL)", "148.60"], ["l", "CARD MACHINE", "86.40"], ["l", "CASH JAR", "31.50"], ["-"],
  ["l", "MARIA  SERVER 6H", "63.96"], ["l", "LEA    SERVER 6H", "63.96"], ["l", "JOHN   SERVER 5.5H", "58.63"],
  ["l", "ALEX   BAR 20%", "53.30"], ["l", "SAM    BUSSER 10%", "26.65"], ["-"],
  ["t", "TO PAY OUT", "$266.50"], ["c", "EXACT CENTS · ADDS UP", 20], [" "], ["stamp"], [" "], [" "],
];
const printer = {
  lines: still ? RECEIPT.length : 0, len: 0.05, started: 0, active: false,
  start() {
    if (still) { this.lines = RECEIPT.length; this.draw(); this.len = Math.min(1, (this.used + 20) / PAPER_H); this.active = false; return; }
    this.lines = 0; this.len = 0.05; this.started = performance.now(); this.active = true; this.draw();
  },
  draw() {
    const p = paperCanvas.getContext("2d");
    p.fillStyle = "#f4efe3"; p.fillRect(0, 0, PAPER_W, PAPER_H);
    p.textBaseline = "top"; let y = 26;
    RECEIPT.slice(0, this.lines).forEach(([kind, a, b]) => {
      p.fillStyle = "#1b1712";
      if (kind === "-") { p.fillStyle = "#9c9486"; for (let x = 18; x < PAPER_W - 18; x += 12) p.fillRect(x, y + 12, 6, 2); y += 30; return; }
      if (kind === " ") { y += 26; return; }
      if (kind === "stamp") {
        p.save(); p.translate(PAPER_W / 2, y + 40); p.rotate(-0.22);
        p.strokeStyle = "#1f8a4c"; p.fillStyle = "#1f8a4c"; p.lineWidth = 5; p.strokeRect(-96, -34, 192, 72);
        p.font = '700 46px "IBM Plex Mono", monospace'; p.textAlign = "center"; p.fillText("PAID", 0, -30);
        p.font = '500 16px "IBM Plex Mono", monospace'; p.fillText("VIA PAYPAL", 0, 18); p.restore();
        y += 100; return;
      }
      if (kind === "c") { p.font = `700 ${b}px "IBM Plex Mono", monospace`; p.textAlign = "center"; p.fillText(a, PAPER_W / 2, y); y += b + 12; return; }
      const big = kind === "t";
      p.font = `${big ? 700 : 500} ${big ? 24 : 19}px "IBM Plex Mono", monospace`;
      p.textAlign = "left"; p.fillText(a, 22, y);
      p.textAlign = "right";
      if (big) { p.fillStyle = "#ffd84a"; const w = p.measureText(b).width; p.fillRect(PAPER_W - 26 - w, y - 2, w + 8, 30); p.fillStyle = "#1b1712"; }
      p.fillText(b, PAPER_W - 22, y); y += big ? 40 : 30;
    });
    this.used = y;
  },
  tick(now) {  // one line every ~0.33s; the paper comes out as it prints
    if (!this.active) return false;
    const want = Math.min(RECEIPT.length, Math.floor((now - this.started) / 330));
    let changed = false;
    if (want > this.lines) { this.lines = want; this.draw(); changed = true; }
    const target = Math.max(0.05, (this.used + 20) / PAPER_H);
    if (this.len < target - 0.001) { this.len += (target - this.len) * 0.12; changed = true; }
    if (this.lines >= RECEIPT.length && this.len >= target - 0.002) this.active = false;
    return changed;
  },
};
printer.draw();

/* ---------------- no WebGL (or it fails): the same screen, flat ---------------- */
function flat() {
  sceneHost.hidden = true; flatHost.hidden = false;
  screen.className = "tp-flat-screen";
  flatHost.prepend(screen);
  screen.addEventListener("click", (e) => {
    const r = screen.getBoundingClientRect();
    pickAt((e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height, true);
  });
  screen.addEventListener("mousemove", (e) => {
    const r = screen.getBoundingClientRect();
    screen.style.cursor = pickAt((e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height, false) ? "pointer" : "default";
  });
  const loop = (now) => { drawScreen(now); requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
}

/* ---------------- the 3D scene ---------------- */
async function scene3d() {
  const THREE = await import(page.dataset.three);
  const renderer = new THREE.WebGLRenderer({antialias: true, powerPreference: "high-performance"});
  const small = Math.min(window.innerWidth, window.innerHeight) < 600;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, small ? 1.5 : 1.75));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  sceneHost.appendChild(renderer.domElement);
  renderer.domElement.setAttribute("aria-hidden", "true");

  const scene = new THREE.Scene();
  scene.background = new THREE.Color("#07060a");
  scene.fog = new THREE.FogExp2("#07060a", 0.045);
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 200);

  // materials
  const plastic = new THREE.MeshStandardMaterial({color: "#d9cfbb", roughness: 0.55, metalness: 0.02});
  const plasticDark = new THREE.MeshStandardMaterial({color: "#b9ad96", roughness: 0.6});
  const charcoal = new THREE.MeshStandardMaterial({color: "#26231f", roughness: 0.5, metalness: 0.1});
  const black = new THREE.MeshStandardMaterial({color: "#0b0a09", roughness: 0.35});

  const rounded = (w, h, d, r, mat) => {  // a box with rounded edges (extruded rounded rectangle)
    const s = new THREE.Shape(), x = -w / 2 + r, y = -h / 2 + r, ww = w - 2 * r, hh = h - 2 * r;
    s.moveTo(x, y - r); s.lineTo(x + ww, y - r); s.quadraticCurveTo(x + ww + r, y - r, x + ww + r, y);
    s.lineTo(x + ww + r, y + hh); s.quadraticCurveTo(x + ww + r, y + hh + r, x + ww, y + hh + r);
    s.lineTo(x, y + hh + r); s.quadraticCurveTo(x - r, y + hh + r, x - r, y + hh); s.lineTo(x - r, y);
    s.quadraticCurveTo(x - r, y - r, x, y - r);
    const geo = new THREE.ExtrudeGeometry(s, {depth: d - 2 * r * 0.6, bevelEnabled: true, bevelThickness: r * 0.6, bevelSize: r * 0.6,
                                              bevelSegments: 4, curveSegments: 8});
    geo.translate(0, 0, -(d - 2 * r * 0.6) / 2);
    const mesh = new THREE.Mesh(geo, mat);
    mesh.castShadow = mesh.receiveShadow = true;
    return mesh;
  };

  // the bar counter, with a little wood grain
  const wood = document.createElement("canvas"); wood.width = 512; wood.height = 512;
  const wg = wood.getContext("2d");
  wg.fillStyle = "#2b1d13"; wg.fillRect(0, 0, 512, 512);
  for (let i = 0; i < 140; i++) {
    wg.strokeStyle = `rgba(${18 + Math.random() * 30},${10 + Math.random() * 18},${6 + Math.random() * 10},${0.25 + Math.random() * 0.35})`;
    wg.lineWidth = 1 + Math.random() * 3; wg.beginPath();
    const y = Math.random() * 512; wg.moveTo(0, y);
    for (let x = 0; x <= 512; x += 32) wg.lineTo(x, y + Math.sin(x / 70 + i) * 6);
    wg.stroke();
  }
  const woodTex = new THREE.CanvasTexture(wood); woodTex.colorSpace = THREE.SRGBColorSpace;
  woodTex.wrapS = woodTex.wrapT = THREE.RepeatWrapping; woodTex.repeat.set(5, 4);
  const counter = new THREE.Mesh(new THREE.BoxGeometry(70, 0.8, 50), new THREE.MeshStandardMaterial({map: woodTex, roughness: 0.42, metalness: 0.05}));
  counter.position.set(0, -0.4, 14); counter.receiveShadow = true; scene.add(counter);

  // the terminal: base, slanted monitor, keypad
  const term = new THREE.Group(); scene.add(term);
  const base = rounded(6.6, 1.1, 5.4, 0.25, plastic); base.position.set(0, 0.55, 0); term.add(base);
  const monitor = new THREE.Group(); monitor.position.set(0, 1.1, -0.9); monitor.rotation.x = -0.12; term.add(monitor);
  const body = rounded(6.2, 4.7, 3.6, 0.35, plastic); body.position.set(0, 2.35, 0); monitor.add(body);
  const bezel = rounded(5.5, 4.0, 0.3, 0.18, plasticDark); bezel.position.set(0, 2.45, 1.72); monitor.add(bezel);
  const recess = new THREE.Mesh(new THREE.PlaneGeometry(4.9, 3.5), black); recess.position.set(0, 2.45, 1.86); monitor.add(recess);

  // the CRT: a slightly bulging screen drawn through a CRT shader
  const screenTex = new THREE.CanvasTexture(screen);
  screenTex.colorSpace = THREE.SRGBColorSpace; screenTex.minFilter = THREE.LinearFilter; screenTex.generateMipmaps = false;
  const crtGeo = new THREE.PlaneGeometry(4.66, 3.46, 32, 24);
  const pos = crtGeo.attributes.position;
  for (let i = 0; i < pos.count; i++) {
    const x = pos.getX(i) / 2.33, y = pos.getY(i) / 1.73;
    pos.setZ(i, 0.16 * (1 - (x * x * 0.55 + y * y * 0.55)));
  }
  crtGeo.computeVertexNormals();
  const crt = new THREE.Mesh(crtGeo, new THREE.ShaderMaterial({
    uniforms: {map: {value: screenTex}, time: {value: 0}, power: {value: 0}},
    vertexShader: "varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
    fragmentShader: `
      uniform sampler2D map; uniform float time; uniform float power; varying vec2 vUv;
      vec2 curve(vec2 uv){ uv = uv * 2.0 - 1.0; vec2 o = abs(uv.yx) / vec2(5.5, 4.5); uv += uv * o * o; return uv * 0.5 + 0.5; }
      float hash(vec2 p){ return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
      void main(){
        vec2 uv = curve(vUv);
        if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) { gl_FragColor = vec4(0.012, 0.01, 0.008, 1.0); return; }
        float ab = 0.0016;
        vec3 c = vec3(texture2D(map, uv + vec2(ab, 0.0)).r, texture2D(map, uv).g, texture2D(map, uv - vec2(ab, 0.0)).b);
        vec3 glow = (texture2D(map, uv + vec2(0.004, 0.0)).rgb + texture2D(map, uv - vec2(0.004, 0.0)).rgb
                   + texture2D(map, uv + vec2(0.0, 0.006)).rgb + texture2D(map, uv - vec2(0.0, 0.006)).rgb) * 0.25;
        c += glow * 0.45;
        c *= 0.74 + 0.26 * sin(uv.y * 768.0 * 3.14159);
        c *= 0.93 + 0.07 * sin(uv.x * 1024.0 * 2.0944);
        float vig = 16.0 * uv.x * uv.y * (1.0 - uv.x) * (1.0 - uv.y); c *= pow(vig, 0.28);
        float roll = smoothstep(0.0, 0.08, abs(fract(uv.y * 0.6 - time * 0.07) - 0.5)); c *= 0.94 + 0.06 * roll;
        c += (hash(uv * 900.0 + time) - 0.5) * 0.035;
        c *= (0.97 + 0.03 * sin(time * 50.0)) * power;
        gl_FragColor = vec4(c * 1.25, 1.0);
      }`,
  }));
  crt.position.set(0, 2.45, 1.9); monitor.add(crt);

  // a name plate under the screen
  const plateCanvas = document.createElement("canvas"); plateCanvas.width = 512; plateCanvas.height = 64;
  const pg = plateCanvas.getContext("2d");
  pg.fillStyle = "#b9ad96"; pg.fillRect(0, 0, 512, 64);
  pg.fillStyle = "#3a2f22"; pg.font = 'italic 600 40px "STIX Two Text", Georgia, serif'; pg.textBaseline = "middle"; pg.fillText("TipPool", 16, 34);
  pg.font = '500 22px "IBM Plex Mono", monospace'; pg.textAlign = "right"; pg.fillText("POS-80", 496, 34);
  const plateTex = new THREE.CanvasTexture(plateCanvas); plateTex.colorSpace = THREE.SRGBColorSpace;
  const plate = new THREE.Mesh(new THREE.PlaneGeometry(2.6, 0.325), new THREE.MeshStandardMaterial({map: plateTex, roughness: 0.6}));
  plate.position.set(-1.25, 0.3, 1.88); monitor.add(plate);
  const led = new THREE.Mesh(new THREE.CircleGeometry(0.07, 16), new THREE.MeshBasicMaterial({color: "#45e08c"}));
  led.position.set(2.45, 0.3, 1.88); monitor.add(led);

  // the keypad, tilted toward the person at the till; PAY in yellow
  const pad = new THREE.Group(); pad.position.set(0, 1.12, 1.75); pad.rotation.x = 0.16; term.add(pad);
  const keyGeo = new THREE.BoxGeometry(0.5, 0.2, 0.42);
  const keyMat = new THREE.MeshStandardMaterial({color: "#ebe3d2", roughness: 0.5});
  const keys = new THREE.InstancedMesh(keyGeo, keyMat, 30); keys.castShadow = true;
  const m4 = new THREE.Matrix4(); let k = 0;
  for (let row = 0; row < 3; row++) for (let col = 0; col < 10; col++) {
    m4.makeTranslation(-2.7 + col * 0.6, 0.1, -0.55 + row * 0.55); keys.setMatrixAt(k++, m4);
  }
  pad.add(keys);
  const payKey = new THREE.Mesh(new THREE.BoxGeometry(1.1, 0.22, 0.97), new THREE.MeshStandardMaterial({color: "#ffd84a", roughness: 0.4, emissive: "#3a2a00"}));
  payKey.position.set(2.35, 0.11, -0.28); payKey.castShadow = true; pad.add(payKey);
  keys.position.x = -0.35;

  // the receipt printer, with paper coming out of it
  const printerBox = rounded(2.6, 1.5, 2.8, 0.22, charcoal); printerBox.position.set(5.2, 0.75, 0.6); scene.add(printerBox);
  const slot = new THREE.Mesh(new THREE.BoxGeometry(2.0, 0.06, 0.12), black); slot.position.set(5.2, 1.51, 0.35); scene.add(slot);
  const prLed = new THREE.Mesh(new THREE.CircleGeometry(0.06, 12), new THREE.MeshBasicMaterial({color: "#45e08c"}));
  prLed.position.set(6.2, 1.0, 2.01); scene.add(prLed);
  const paperTex = new THREE.CanvasTexture(paperCanvas); paperTex.colorSpace = THREE.SRGBColorSpace; paperTex.anisotropy = 4;
  const paperGeo = new THREE.PlaneGeometry(1.75, 1, 1, 40);
  const paper = new THREE.Mesh(paperGeo, new THREE.MeshStandardMaterial({map: paperTex, roughness: 0.85, side: THREE.DoubleSide}));
  paper.castShadow = true; paper.position.set(5.2, 1.52, 0.35); scene.add(paper);
  const PAPER_LEN = 5.4;
  function shapePaper() {  // rises out of the slot, leaning back and curling over as it gets longer
    const h = printer.len * PAPER_LEN, p = paperGeo.attributes.position, uv = paperGeo.attributes.uv;
    for (let i = 0; i < p.count; i++) {
      const t = 1 - uv.getY(i);          // 0 at the top edge (printed first) .. 1 at the slot
      const s = (1 - t) * h;             // distance from the slot along the paper
      p.setY(i, s * 0.93 - 0.03 * s * s);
      p.setZ(i, -0.1 * s - 0.09 * s * s);
    }
    p.needsUpdate = true; paperGeo.computeVertexNormals();
    paperTex.repeat.set(1, printer.len); paperTex.offset.set(0, 1 - printer.len); paperTex.needsUpdate = true;
  }
  shapePaper();

  // light: a warm lamp over the bar, the screen's glow, a cold rim from behind
  scene.add(new THREE.AmbientLight("#2a2230", 0.9));
  const lamp = new THREE.SpotLight("#ffd7a0", 260, 40, 0.48, 0.7, 1.4);
  lamp.position.set(-5, 13, 7); lamp.target.position.set(1, 1, 0); lamp.castShadow = true;
  lamp.shadow.mapSize.set(small ? 1024 : 2048, small ? 1024 : 2048); lamp.shadow.bias = -0.0004;
  scene.add(lamp, lamp.target);
  const glow = new THREE.PointLight("#ffaa44", 0, 9, 1.6); glow.position.set(0, 3.6, 3.2); scene.add(glow);
  const rim = new THREE.DirectionalLight("#6f86c9", 0.35); rim.position.set(4, 6, -10); scene.add(rim);

  // smoke drifting through the light
  const smokeCanvas = document.createElement("canvas"); smokeCanvas.width = smokeCanvas.height = 256;
  const sg = smokeCanvas.getContext("2d");
  for (let i = 0; i < 60; i++) {
    const x = 128 + (Math.random() - 0.5) * 120, y = 128 + (Math.random() - 0.5) * 120, r = 30 + Math.random() * 70;
    const grad = sg.createRadialGradient(x, y, 0, x, y, r);
    grad.addColorStop(0, "rgba(255,255,255,0.07)"); grad.addColorStop(1, "rgba(255,255,255,0)");
    sg.fillStyle = grad; sg.fillRect(0, 0, 256, 256);
  }
  const smokeTex = new THREE.CanvasTexture(smokeCanvas);
  const smoke = [];
  for (let i = 0; i < 9; i++) {
    const s = new THREE.Sprite(new THREE.SpriteMaterial({map: smokeTex, color: i % 3 ? "#b8a7c9" : "#ffcf9a", transparent: true,
      opacity: 0.18 + Math.random() * 0.14, depthWrite: false, blending: THREE.AdditiveBlending}));
    const size = 9 + Math.random() * 9; s.scale.set(size, size * 0.7, 1);
    s.position.set((Math.random() - 0.5) * 22, 2 + Math.random() * 6, -6 + Math.random() * 6);
    s.userData = {speed: 0.08 + Math.random() * 0.15, phase: Math.random() * 6.28, spin: (Math.random() - 0.5) * 0.04};
    smoke.push(s); scene.add(s);
  }

  // framing: the whole terminal and printer on wide screens, the screen up close on narrow ones
  const look = new THREE.Vector3(1.0, 2.6, 0);
  let home = new THREE.Vector3();
  function frame() {
    const w = sceneHost.clientWidth, h = sceneHost.clientHeight;
    renderer.setSize(w, h, false); camera.aspect = w / h;
    const portrait = w / h < 0.9;
    camera.fov = portrait ? 38 : 30;
    camera.updateProjectionMatrix();
    // far enough back that this much of the scene fits both across and down
    const tablet = portrait && w / h > 0.65;
    const fitW = tablet ? 11.5 : portrait ? 8.8 : 15.5, fitH = portrait ? 9.5 : 8;
    const half = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const dist = Math.max(fitW / 2 / (half * camera.aspect), fitH / 2 / half);
    look.set(tablet ? 1.4 : portrait ? 0.4 : -1.3, portrait ? 1.2 : 2.4, 0.3);
    home = new THREE.Vector3(look.x + (portrait ? -0.6 : -3.2), look.y + (portrait ? 1.1 : 2.6), look.z + dist);
    camera.position.copy(home); camera.lookAt(look);
  }
  frame();
  new ResizeObserver(frame).observe(sceneHost);

  // pointer: a gentle parallax, and the screen's menu under the cursor or finger
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2(), target = new THREE.Vector2();
  const pointAt = (e, click) => {
    const r = renderer.domElement.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    target.set(ndc.x, ndc.y);
    ray.setFromCamera(ndc, camera);
    const hitScreen = ray.intersectObject(crt)[0];
    if (hitScreen) { renderer.domElement.style.cursor = pickAt(hitScreen.uv.x, 1 - hitScreen.uv.y, click) ? "pointer" : "default"; return; }
    const hitPrinter = ray.intersectObjects([printerBox, paper])[0];
    renderer.domElement.style.cursor = hitPrinter ? "pointer" : "default";
    if (hitPrinter && click) { printer.start(); }
  };
  renderer.domElement.addEventListener("pointermove", (e) => pointAt(e, false));
  renderer.domElement.addEventListener("click", (e) => pointAt(e, true));

  // flying into the screen before leaving for Log in, Sign up or the demo
  let fly = null;
  flyTo = (done) => { document.body.classList.add("tp-leaving"); fly = {from: camera.position.clone(), start: performance.now(), done}; };
  // coming back with the Back button: the terminal is back at its menu, not mid-flight
  window.addEventListener("pageshow", (e) => { if (e.persisted) { fly = null; camera.position.copy(home); camera.lookAt(look); } });
  const screenFront = new THREE.Vector3(); crt.getWorldPosition(screenFront);
  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

  // the printer prints tonight's tip-out once, after the screen has booted
  let printedOnce = still;
  let last = 0, powerOn = still ? 1 : 0;
  const loop = (now) => {
    const t = still ? 0 : now / 1000;  // with reduced motion: no flicker, no drift, but the screen still answers
    requestAnimationFrame(loop);
    if (document.hidden) return;
    // the screen: a power-on glow, then TIPPOOL OS
    powerOn = Math.min(1, powerOn + 0.02);
    crt.material.uniforms.power.value = powerOn;
    crt.material.uniforms.time.value = t;
    glow.intensity = (14 + Math.sin(t * 50) * 0.6) * powerOn;
    if (os.dirty || now - last > 120) { drawScreen(now); screenTex.needsUpdate = true; last = now; os.dirty = false; }
    if (!printedOnce && os.mode === "menu") { printedOnce = true; setTimeout(() => printer.start(), 900); }
    if (printer.tick(now)) shapePaper();
    if (!still) smoke.forEach((s) => {
      s.position.x += s.userData.speed * 0.01; if (s.position.x > 14) s.position.x = -14;
      s.position.y += Math.sin(t * 0.3 + s.userData.phase) * 0.002; s.material.rotation += s.userData.spin * 0.01;
    });
    if (fly) {
      const p = Math.min(1, (now - fly.start) / 1100), e = ease(p);
      const to = screenFront.clone().add(new THREE.Vector3(0, 0, 1.1));
      camera.position.lerpVectors(fly.from, to, e);
      camera.lookAt(look.clone().lerp(screenFront, e));
      if (p >= 1 && fly.done) { const d = fly.done; fly.done = null; d(); }
    } else if (!still) {
      camera.position.x += (home.x + target.x * 0.9 + Math.sin(t * 0.2) * 0.25 - camera.position.x) * 0.04;
      camera.position.y += (home.y + target.y * 0.5 - camera.position.y) * 0.04;
      camera.lookAt(look);
    }
    renderer.render(scene, camera);
  };
  if (still) { drawScreen(performance.now()); screenTex.needsUpdate = true; shapePaper(); }
  requestAnimationFrame(loop);
  document.documentElement.classList.add("tp-3d");
}

/* ---------------- start: fonts first (the screen is drawn in them), then 3D or flat ---------------- */
(async () => {
  try { await Promise.race([Promise.all(['34px "VT323"', '20px "IBM Plex Mono"', 'italic 40px "STIX Two Text"'].map((f) => document.fonts.load(f))),
                            new Promise((r) => setTimeout(r, 2500))]); } catch (e) { /* draw with the fallback fonts */ }
  if (still) { os.mode = "menu"; printer.start(); }
  printer.draw();
  const gl = (() => { try { const c = document.createElement("canvas"); return !!(c.getContext("webgl2") || c.getContext("webgl")); } catch (e) { return false; } })();
  if (!gl) { flat(); return; }
  try { await scene3d(); } catch (e) { console.error(e); flat(); }
})();
window.addEventListener("pageshow", (e) => {
  if (!e.persisted) return;
  document.body.classList.remove("tp-leaving");
  os.busy = null; setMode("menu");
});
