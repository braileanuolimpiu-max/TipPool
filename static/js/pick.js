/* Dropdown lists in TipPool's own style.
   The <select> box stays as it is (so forms, auto-submit and the styles around it keep working);
   only the list that opens is ours instead of the browser's. On touch screens it's a sheet from the bottom with
   rows big enough for a thumb. Add data-native to a <select> to keep the browser's list. */
(() => {
  const touch = window.matchMedia("(pointer: coarse)").matches;

  let open = null;      // {select, list, items, active}
  let typed = "", typedAt = 0;
  let uid = 0;

  const usable = (s) => s && s.tagName === "SELECT" && !s.multiple && !s.disabled && !("native" in s.dataset);
  const enabledItems = () => open.items.filter((li) => !li.hasAttribute("aria-disabled"));

  function close(refocus) {
    if (!open) return;
    const {select, list, scrim} = open;
    list.remove();
    if (scrim) scrim.remove();
    select.removeAttribute("aria-activedescendant");
    select.classList.remove("pick-open");
    open = null;
    if (refocus && !touch) select.focus({preventScroll: true});
  }

  function choose(select, index) {
    if (select.selectedIndex !== index) {
      select.selectedIndex = index;
      select.dispatchEvent(new Event("input", {bubbles: true}));
      select.dispatchEvent(new Event("change", {bubbles: true}));
    }
    close(true);
  }

  function setActive(li) {
    if (!li || !open) return;
    if (open.active) open.active.classList.remove("active");
    open.active = li;
    li.classList.add("active");
    li.scrollIntoView({block: "nearest"});
    open.select.setAttribute("aria-activedescendant", li.id);
  }

  function build(select) {
    const list = document.createElement("ul");
    list.className = touch ? "pick-list pick-sheet" : "pick-list";
    list.setAttribute("role", "listbox");
    if (!touch) list.style.fontSize = getComputedStyle(select).fontSize;
    const items = [];
    const name = select.getAttribute("aria-label") || (select.labels && select.labels[0] ? select.labels[0].textContent.trim() : "");
    if (touch && name) {  // the sheet covers the box it belongs to: say what's being picked
      const title = document.createElement("li");
      title.className = "pick-title";
      title.setAttribute("role", "presentation");
      title.textContent = name;
      list.appendChild(title);
    }
    let group = null;
    [...select.options].forEach((opt, i) => {
      const parent = opt.parentElement;
      if (parent.tagName === "OPTGROUP" && parent !== group) {
        group = parent;
        const head = document.createElement("li");
        head.className = "pick-group";
        head.setAttribute("role", "presentation");
        head.textContent = parent.label;
        list.appendChild(head);
      }
      const li = document.createElement("li");
      li.className = "pick-opt";
      li.id = `pick-${++uid}`;
      li.setAttribute("role", "option");
      li.dataset.index = i;
      li.textContent = opt.text;
      if (opt.disabled) li.setAttribute("aria-disabled", "true");
      if (i === select.selectedIndex) li.setAttribute("aria-selected", "true");
      list.appendChild(li);
      items.push(li);
    });
    list.addEventListener("mousedown", (e) => e.preventDefault());  // focus stays on the select
    list.addEventListener("click", (e) => {
      const li = e.target.closest(".pick-opt");
      if (li && !li.hasAttribute("aria-disabled")) choose(select, Number(li.dataset.index));
    });
    list.addEventListener("mousemove", (e) => {
      const li = e.target.closest(".pick-opt");
      if (li && li !== open.active && !li.hasAttribute("aria-disabled")) setActive(li);
    });
    return {list, items};
  }

  function place(select, list) {
    if (touch) return;  // the sheet's place is in the stylesheet: the bottom of the screen
    const r = select.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - 12, above = r.top - 12;
    list.style.width = `${Math.min(r.width, document.documentElement.clientWidth - 16)}px`;  // exactly as wide as its box
    const upward = below < 220 && above > below;
    list.style.maxHeight = `${Math.min(320, upward ? above : below)}px`;
    if (upward) list.style.bottom = `${window.innerHeight - r.top + 4}px`;
    else list.style.top = `${r.bottom + 4}px`;
    const width = list.offsetWidth;
    list.style.left = `${Math.max(8, Math.min(r.left, document.documentElement.clientWidth - width - 8))}px`;
  }

  function openFor(select) {
    close();
    const {list, items} = build(select);
    let scrim = null;
    if (touch) {  // a tap outside the sheet closes it
      scrim = document.createElement("div");
      scrim.className = "pick-scrim";
      scrim.addEventListener("click", () => close());
      document.body.appendChild(scrim);
    }
    document.body.appendChild(list);
    open = {select, list, items, active: null, scrim};
    place(select, list);
    select.classList.add("pick-open");
    setActive(items[select.selectedIndex] && !items[select.selectedIndex].hasAttribute("aria-disabled")
      ? items[select.selectedIndex] : enabledItems()[0]);
  }

  function move(step) {
    const enabled = enabledItems();
    if (!enabled.length) return;
    let at = enabled.indexOf(open.active) + step;
    at = Math.max(0, Math.min(enabled.length - 1, at));
    setActive(enabled[at]);
  }

  function typeAhead(char) {
    const now = Date.now();
    typed = now - typedAt > 700 ? char : typed + char;
    typedAt = now;
    const enabled = enabledItems();
    const start = enabled.indexOf(open.active);
    const order = [...enabled.slice(start + (typed.length === 1 ? 1 : 0)), ...enabled.slice(0, start + 1)];
    const hit = order.find((li) => li.textContent.trim().toLowerCase().startsWith(typed.toLowerCase()));
    if (hit) setActive(hit);
  }

  document.addEventListener("mousedown", (e) => {
    const select = e.target.closest && e.target.closest("select");
    if (e.button === 0 && usable(select)) {
      e.preventDefault();  // no browser list
      select.focus({preventScroll: true});
      if (open && open.select === select) close();
      else openFor(select);
      return;
    }
    if (open && !open.list.contains(e.target)) close();
  }, true);

  document.addEventListener("keydown", (e) => {
    if (!open) {
      const select = e.target;
      if (usable(select) && (e.key === " " || e.key === "Enter" || e.key === "F4" || (e.altKey && e.key === "ArrowDown"))) {
        e.preventDefault();
        openFor(select);
      }
      return;
    }
    const keys = {
      ArrowDown: () => move(1), ArrowUp: () => move(-1), PageDown: () => move(8), PageUp: () => move(-8),
      Home: () => move(-Infinity), End: () => move(Infinity),
      Enter: () => open.active && choose(open.select, Number(open.active.dataset.index)),
      " ": () => (typed && Date.now() - typedAt < 700 ? typeAhead(" ")
        : open.active && choose(open.select, Number(open.active.dataset.index))),
      Escape: () => close(true),
    };
    if (keys[e.key]) {
      e.preventDefault();
      keys[e.key]();
    } else if (e.key === "Tab") {
      close();
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      typeAhead(e.key);
    }
  }, true);

  // a box too narrow for its choice cuts it short with "…": hovering shows the whole text
  const showFull = (s) => {
    if (!(s instanceof HTMLSelectElement)) return;
    const text = s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : "";
    if (s.scrollWidth > s.clientWidth + 1 || text.length > 28) s.title = text;
    else s.removeAttribute("title");
  };
  document.addEventListener("change", (e) => showFull(e.target), true);
  const titleAll = () => document.querySelectorAll("select").forEach(showFull);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", titleAll);
  else titleAll();

  // touch: a tap on a dropdown opens the sheet (and no browser list); a swipe that starts on one still scrolls
  if (touch) {
    let tap = null;
    document.addEventListener("touchstart", (e) => {
      const select = e.target.closest && e.target.closest("select");
      tap = usable(select) ? {select, x: e.touches[0].clientX, y: e.touches[0].clientY, moved: false} : null;
    }, {capture: true, passive: true});
    document.addEventListener("touchmove", (e) => {
      if (tap && (Math.abs(e.touches[0].clientX - tap.x) > 8 || Math.abs(e.touches[0].clientY - tap.y) > 8)) tap.moved = true;
    }, {capture: true, passive: true});
    document.addEventListener("touchend", (e) => {
      const t = tap;
      tap = null;
      if (!t || t.moved) return;
      e.preventDefault();  // no browser list
      if (open && open.select === t.select) close();
      else openFor(t.select);
    }, {capture: true, passive: false});
  }

  window.addEventListener("resize", () => { if (!touch) close(); });  // a phone's keyboard or toolbar resizing isn't a reason
  window.addEventListener("scroll", (e) => { if (open && !touch && !open.list.contains(e.target)) close(); }, true);
})();
