/* TipPool's front page: the night's story as one scroll film. Each chapter in the page's list becomes a scene;
   scrolling scrubs its video (scroll-world.js). The list itself stays as the page if scripts don't run. */
(() => {
  const list = document.getElementById("lp-chapters");
  const world = document.getElementById("world");
  if (!list || !world || typeof window.mountScrollWorld !== "function") {
    document.documentElement.classList.remove("js");  // show the plain chapters instead
    return;
  }
  const sections = [...list.children].map((li, i) => {
    const d = li.dataset;
    return {
      id: `chapter-${i + 1}`, label: d.label, accent: d.accent,
      still: d.still, stillMobile: d.stillM, clip: d.clip,
      seqBase: d.seq, seqCount: d.seqN ? Number(d.seqN) : 0,  // phones: still frames instead of the video
      eyebrow: d.eyebrow, title: li.querySelector("h1, h2").textContent, body: li.querySelector("p").textContent,
      tags: d.tags ? d.tags.split("|") : [],
      scroll: d.scroll ? Number(d.scroll) : undefined, linger: d.linger ? Number(d.linger) : undefined,
      ctaFrom: d.cta || undefined,
    };
  });
  window.mountScrollWorld(world, {
    nav: false, atmosphere: true, hint: "scroll", diveScroll: 2.4, crossfade: 0.3, sections,
  });
  // the first chapter's title is the page's main heading
  const first = world.querySelector(".sw-copy__title");
  if (first) {
    const h1 = document.createElement("h1");
    h1.className = first.className;
    h1.textContent = first.textContent;
    first.replaceWith(h1);
  }
})();

/* past the film, the top bar gets a solid backing */
(() => {
  const bar = document.querySelector(".lp-bar"), after = document.querySelector(".lp-after");
  if (!bar || !after) return;
  let queued = false;
  const check = () => { queued = false; bar.classList.toggle("solid", after.getBoundingClientRect().top <= bar.offsetHeight); };
  window.addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(check); } }, {passive: true});
  check();
})();
