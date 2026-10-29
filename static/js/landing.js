/* TipPool's front page: the receipt that prints itself, the agent chat, counters and reveals.
   Everything pauses off screen; people who asked for less motion get the finished picture at once. */
(() => {
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const money = (v, prefix = "$", decimals = 2) => prefix + v.toLocaleString("en-US", {minimumFractionDigits: decimals, maximumFractionDigits: decimals});

  function countUp(el, to, {prefix = "$", decimals = 2, ms = 450} = {}) {
    if (still) { el.textContent = money(to, prefix, decimals); return; }
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / ms), eased = 1 - Math.pow(1 - t, 3);
      el.textContent = money(to * eased, prefix, decimals);
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // whether a part of the page is on screen (animations wait while it isn't)
  const onScreen = new WeakMap();
  const watch = new IntersectionObserver((entries) => entries.forEach((e) => onScreen.set(e.target, e.isIntersecting)), {threshold: 0.15});
  const until = async (el) => { while (!onScreen.get(el) || document.hidden) await wait(300); };

  /* ---------- the hero: a guest tips, the receipt prints, the shift is paid ---------- */
  const receipt = $("#lp-receipt"), phone = $("#lp-phone");
  if (receipt && phone) {
    const stage = receipt.closest(".lp-stage");
    watch.observe(stage);
    const steps = $$("[data-step]", receipt).sort((a, b) => a.dataset.step - b.dataset.step);
    const pick = $(".lp-pick", phone), pay = $(".lp-ph-pay", phone), thanks = $(".lp-ph-thanks", phone);
    const stamp = $("#lp-stamp"), floats = $$(".lp-float", stage);
    const reveal = (el) => {  // print the paper down to just below this line
      const cut = receipt.offsetHeight - (el.offsetTop + el.offsetHeight + 14);
      receipt.style.setProperty("--cut", `${Math.max(0, cut)}px`);
    };
    const finish = () => {
      receipt.style.setProperty("--cut", "0px");
      steps.forEach((el) => { el.classList.add("on"); const b = el.querySelector("b[data-count]"); if (b) b.textContent = money(Number(b.dataset.count)); });
      stamp.classList.add("on");
      floats.forEach((f) => f.classList.add("on"));
    };
    const reset = () => {
      receipt.style.setProperty("--cut", "100%");
      steps.forEach((el) => el.classList.remove("on"));
      [stamp, pick, pay, thanks, ...floats].forEach((el) => el.classList.remove("on"));
      $$("b[data-count]", receipt).forEach((b) => { b.textContent = "$0.00"; });
    };
    if (still) finish();
    else (async () => {
      for (;;) {
        await until(stage);
        reset();
        await wait(700); pick.classList.add("on");
        await wait(800); pay.classList.add("on");
        await wait(500); pay.classList.remove("on"); thanks.classList.add("on");
        await wait(600);
        reveal($(".lp-r-head", receipt));
        for (const el of steps) {
          await wait(el.classList.contains("lp-r-rule") ? 260 : 430);
          el.classList.add("on");
          reveal(el);
          const b = el.querySelector("b[data-count]");
          if (b) countUp(b, Number(b.dataset.count));
          if (el.dataset.step === "4") floats[0].classList.add("on");
          if (el.dataset.step === "5") { thanks.classList.remove("on"); pick.classList.remove("on"); }
        }
        receipt.style.setProperty("--cut", "0px");
        await wait(500); stamp.classList.add("on");
        await wait(400); floats[1].classList.add("on");
        await wait(4200);
      }
    })();
  }

  /* ---------- the agent: a request becomes a plan, confirmed once, paid ---------- */
  const chat = $("#lp-chat");
  if (chat) {
    watch.observe(chat);
    const typed = $("#lp-typed"), plan = $("#lp-plan"), done = $("#lp-done");
    const planSteps = $$(".lp-plan-step", plan), confirm = $(".lp-confirm", plan);
    const text = "Close out tonight, add $40 cash and pay everyone";
    const finish = () => {
      typed.textContent = text;
      [plan, ...planSteps, confirm, done].forEach((el) => el.classList.add("on"));
      confirm.classList.add("done");
    };
    if (still) finish();
    else (async () => {
      for (;;) {
        await until(chat);
        typed.textContent = "";
        [plan, ...planSteps, confirm, done].forEach((el) => el.classList.remove("on", "done"));
        await wait(600);
        for (const ch of text) { typed.textContent += ch; await wait(38); }
        await wait(500); plan.classList.add("on");
        for (const s of planSteps) { await wait(450); s.classList.add("on"); }
        await wait(400); confirm.classList.add("on");
        await wait(1600); confirm.classList.add("done");
        await wait(500); done.classList.add("on");
        await wait(5000);
      }
    })();
  }

  /* ---------- reveal on scroll, and the numbers ---------- */
  const reveals = $$(".lp-reveal");
  if (still || !("IntersectionObserver" in window)) reveals.forEach((el) => el.classList.add("in"));
  else {
    const io = new IntersectionObserver((entries) => entries.forEach((e) => {
      if (!e.isIntersecting) return;
      e.target.classList.add("in");
      e.target.querySelectorAll("[data-count-to]").forEach((b) => countUp(b, Number(b.dataset.countTo),
        {prefix: b.dataset.prefix || "", decimals: Number(b.dataset.decimals || 0), ms: 1200}));
      io.unobserve(e.target);
    }), {threshold: 0.2, rootMargin: "0px 0px -40px 0px"});
    reveals.forEach((el) => io.observe(el));
  }
  if (still) $$("[data-count-to]").forEach((b) => countUp(b, Number(b.dataset.countTo), {prefix: b.dataset.prefix || "", decimals: Number(b.dataset.decimals || 0)}));

  /* ---------- the top bar gets a solid backing once the page scrolls ---------- */
  const nav = $(".lp-nav");
  if (nav) {
    let queued = false;
    const mark = () => { queued = false; nav.classList.toggle("scrolled", window.scrollY > 24); };
    window.addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(mark); } }, {passive: true});
    mark();
  }

  /* ---------- the bottom banner can be closed (and stays closed) ---------- */
  const banner = $("#lp-banner");
  try { if (localStorage.getItem("lp-banner-x") === "1" && banner) banner.hidden = true; } catch (e) {}
  const x = $("#lp-banner-x");
  if (x && banner) x.addEventListener("click", () => {
    banner.hidden = true;
    try { localStorage.setItem("lp-banner-x", "1"); } catch (e) {}
  });
})();
