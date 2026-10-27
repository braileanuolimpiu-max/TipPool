/* The bits browsers draw themselves, redrawn in TipPool's style:
   form error bubbles, the date picker, file pickers and hover tooltips.
   Phones keep their native date picker (easier with a thumb). */
(() => {
  const fine = !window.matchMedia("(pointer: coarse)").matches;

  /* ---------- form errors: a line under the field instead of the browser's bubble ---------- */
  function message(field) {
    const v = field.validity;
    if (v.valueMissing) {
      if (field.type === "checkbox") return "Please tick this to continue.";
      if (field.tagName === "SELECT") return "Please choose one.";
      if (field.type === "file") return "Please choose a file.";
      return "Please fill this in.";
    }
    if (v.typeMismatch && field.type === "email") return "Enter an email address, like name@example.com.";
    if (v.badInput) return field.type === "number" ? "Enter a number." : "Check this value.";
    if (v.rangeUnderflow) return `Use ${field.min} or more.`;
    if (v.rangeOverflow) return `Use ${field.max} or less.`;
    if (v.stepMismatch) return "Use an amount like 12 or 12.50.";
    if (v.tooShort) return `Use at least ${field.minLength} characters.`;
    if (v.tooLong) return `Use at most ${field.maxLength} characters.`;
    return field.validationMessage || "Check this value.";
  }
  function anchor(field) {
    return field.closest(".file-pick, .money-input, .date-wrap") || field;
  }
  function clear(field) {
    const box = anchor(field);
    const note = box.nextElementSibling;
    if (note && note.classList.contains("field-error")) note.remove();
    field.removeAttribute("aria-invalid");
  }
  let focusedThisRound = false;
  document.addEventListener("invalid", (e) => {
    const field = e.target;
    e.preventDefault();  // no browser bubble
    clear(field);
    const note = document.createElement("p");
    note.className = "field-error";
    note.id = `${field.name || "field"}-error-${Math.random().toString(36).slice(2, 7)}`;
    note.textContent = message(field);
    anchor(field).insertAdjacentElement("afterend", note);
    field.setAttribute("aria-invalid", "true");
    field.setAttribute("aria-describedby", note.id);
    if (!focusedThisRound) {  // the first problem gets the focus, once per try
      focusedThisRound = true;
      (field.type === "file" ? anchor(field).querySelector("button") : field).focus({preventScroll: true});
      anchor(field).scrollIntoView({block: "center", behavior: "smooth"});
      setTimeout(() => { focusedThisRound = false; }, 0);
    }
  }, true);
  ["input", "change"].forEach((type) => document.addEventListener(type, (e) => {
    if (e.target.matches && e.target.matches("[aria-invalid]") && e.target.checkValidity()) clear(e.target);
  }, true));

  /* ---------- file pickers ---------- */
  document.querySelectorAll('input[type="file"]').forEach((input) => {
    const wrap = document.createElement("span");
    wrap.className = "file-pick";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-secondary small";
    button.textContent = input.accept && input.accept.includes("image") ? "Choose a photo" : "Choose a file";
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = "Nothing chosen yet";
    wrap.append(button, name);
    button.addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      name.textContent = input.files.length ? [...input.files].map((f) => f.name).join(", ") : "Nothing chosen yet";
      wrap.classList.toggle("has-file", input.files.length > 0);
    });
    const label = input.id && document.querySelector(`label[for="${input.id}"]`);
    if (label) label.addEventListener("click", (e) => { e.preventDefault(); button.focus(); });
  });

  /* ---------- date picker ---------- */
  if (fine) {
    const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                    "November", "December"];
    const DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
    const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const parse = (s) => (/^\d{4}-\d{2}-\d{2}$/.test(s) ? new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) : null);
    let cal = null;  // {input, box, month, focus}

    function closeCal(refocus) {
      if (!cal) return;
      cal.box.remove();
      if (refocus) cal.input.focus({preventScroll: true});
      cal = null;
    }
    function setDate(input, d) {
      input.value = d ? iso(d) : "";
      input.dispatchEvent(new Event("input", {bubbles: true}));
      input.dispatchEvent(new Event("change", {bubbles: true}));
    }
    function render() {
      const {box, month, focus, input} = cal;
      const chosen = parse(input.value), today = new Date();
      const first = new Date(month.getFullYear(), month.getMonth(), 1);
      const start = new Date(first);
      start.setDate(1 - ((first.getDay() + 6) % 7));  // weeks start on Monday
      let html = `<div class="cal-head"><button type="button" class="cal-nav" data-step="-1" aria-label="Previous month">‹</button>
        <strong aria-live="polite">${MONTHS[month.getMonth()]} ${month.getFullYear()}</strong>
        <button type="button" class="cal-nav" data-step="1" aria-label="Next month">›</button></div>
        <div class="cal-grid" role="grid">${DAYS.map((d) => `<span class="cal-dow" aria-hidden="true">${d}</span>`).join("")}`;
      for (let i = 0; i < 42; i++) {
        const d = new Date(start);
        d.setDate(start.getDate() + i);
        const cls = ["cal-day", d.getMonth() !== month.getMonth() ? "out" : "", iso(d) === iso(today) ? "today" : "",
                     chosen && iso(d) === iso(chosen) ? "chosen" : "", iso(d) === iso(focus) ? "focus" : ""].join(" ");
        html += `<button type="button" class="${cls}" data-date="${iso(d)}" tabindex="${iso(d) === iso(focus) ? 0 : -1}"
          aria-label="${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}">${d.getDate()}</button>`;
      }
      html += `</div><div class="cal-foot"><button type="button" class="btn btn-quiet small" data-act="clear">Clear</button>
        <button type="button" class="btn btn-secondary small" data-act="today">Today</button></div>`;
      box.innerHTML = html;
      const f = box.querySelector(".cal-day.focus");
      if (f && box.contains(document.activeElement)) f.focus();
    }
    function openCal(input) {
      closeCal();
      const box = document.createElement("div");
      box.className = "cal-pop";
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-label", "Choose a date");
      document.body.appendChild(box);
      const chosen = parse(input.value) || new Date();
      cal = {input, box, month: new Date(chosen.getFullYear(), chosen.getMonth(), 1), focus: chosen};
      render();
      const r = input.getBoundingClientRect();
      const up = window.innerHeight - r.bottom < box.offsetHeight + 12 && r.top > box.offsetHeight + 12;
      box.style.top = `${up ? r.top - box.offsetHeight - 4 : r.bottom + 4}px`;
      box.style.left = `${Math.max(8, Math.min(r.left, document.documentElement.clientWidth - box.offsetWidth - 8))}px`;
      box.addEventListener("mousedown", (e) => e.preventDefault());
      box.addEventListener("click", (e) => {
        const day = e.target.closest(".cal-day"), nav = e.target.closest(".cal-nav"), act = e.target.closest("[data-act]");
        if (day) { setDate(input, parse(day.dataset.date)); closeCal(true); }
        else if (nav) { cal.month.setMonth(cal.month.getMonth() + Number(nav.dataset.step)); cal.focus = new Date(cal.month); render(); }
        else if (act) { setDate(input, act.dataset.act === "today" ? new Date() : null); closeCal(true); }
      });
      box.querySelector(".cal-day.focus").focus();
    }
    function moveFocus(days, months) {
      const f = new Date(cal.focus);
      if (months) f.setMonth(f.getMonth() + months);
      f.setDate(f.getDate() + days);
      cal.focus = f;
      cal.month = new Date(f.getFullYear(), f.getMonth(), 1);
      render();
      cal.box.querySelector(".cal-day.focus").focus();
    }

    document.querySelectorAll('input[type="date"]').forEach((input) => {
      const wrap = document.createElement("span");
      wrap.className = "date-wrap";
      input.parentNode.insertBefore(wrap, input);
      wrap.appendChild(input);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "date-btn";
      button.setAttribute("aria-label", "Open calendar");
      button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>';
      wrap.appendChild(button);
      button.addEventListener("click", () => (cal && cal.input === input ? closeCal(true) : openCal(input)));
      input.addEventListener("keydown", (e) => {
        if ((e.altKey && e.key === "ArrowDown") || e.key === "F4") { e.preventDefault(); openCal(input); }
      });
    });
    document.addEventListener("mousedown", (e) => {
      if (cal && !cal.box.contains(e.target) && !e.target.closest(".date-btn")) closeCal();
    }, true);
    document.addEventListener("keydown", (e) => {
      if (!cal || !cal.box.contains(document.activeElement)) return;
      const keys = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [-7, 0], ArrowDown: [7, 0],
                    PageUp: [0, -1], PageDown: [0, 1]};
      if (keys[e.key]) { e.preventDefault(); moveFocus(...keys[e.key]); }
      else if (e.key === "Escape") { e.preventDefault(); closeCal(true); }
    }, true);
    window.addEventListener("resize", () => closeCal());
  }

  /* ---------- hover tooltips in our style instead of the browser's grey box ---------- */
  if (fine) {
    let tip = null, timer = null, owner = null;
    const hide = () => {
      clearTimeout(timer);
      if (tip) tip.remove();
      tip = null;
      owner = null;
    };
    document.addEventListener("mouseover", (e) => {
      const el = e.target.closest && e.target.closest("[title], [data-tooltip]");
      if (!el || el === owner) return;
      if (el.hasAttribute("title")) {  // take the title over so the browser doesn't show its own
        const text = el.getAttribute("title");
        el.removeAttribute("title");
        if (!text) return;
        el.dataset.tooltip = text;
        if (el.matches("a, button, select, input") && !el.getAttribute("aria-label")) el.setAttribute("aria-label", text);
      }
      hide();
      owner = el;
      // a tooltip that only repeats what's already written on the element adds nothing
      if ((el.innerText || "").trim().toLowerCase() === el.dataset.tooltip.trim().toLowerCase()) return;
      timer = setTimeout(() => {
        tip = document.createElement("div");
        tip.className = "ui-tip";
        tip.setAttribute("role", "tooltip");
        tip.textContent = el.dataset.tooltip;
        document.body.appendChild(tip);
        const r = el.getBoundingClientRect();
        const above = r.top > tip.offsetHeight + 12;
        tip.classList.toggle("below", !above);
        tip.style.top = `${above ? r.top - tip.offsetHeight - 8 : r.bottom + 8}px`;
        tip.style.left = `${Math.max(8, Math.min(r.left + r.width / 2 - tip.offsetWidth / 2, document.documentElement.clientWidth - tip.offsetWidth - 8))}px`;
      }, 350);
    });
    document.addEventListener("mouseout", (e) => {
      if (owner && (!e.relatedTarget || !owner.contains(e.relatedTarget))) hide();
    });
    ["mousedown", "scroll", "keydown"].forEach((type) => window.addEventListener(type, hide, true));
  }
})();

/* Small behaviours that used to be inline handlers (the Content-Security-Policy blocks those):
   a select marked data-autosubmit sends its form when changed; a copy-link box selects itself when clicked. */
document.addEventListener("change", (e) => {
  const select = e.target.closest && e.target.closest("select[data-autosubmit]");
  if (select && select.form) select.form.requestSubmit();
});
document.addEventListener("click", (e) => {
  if (e.target.matches && e.target.matches("input.copy-link")) e.target.select();
});

/* Styles worked out on the server (a meter's width, a bar's height) come as data-css="...": the
   Content-Security-Policy blocks style attributes in the page, but not styles set from here. */
(() => {
  const apply = (root) => {
    const els = root.querySelectorAll ? root.querySelectorAll("[data-css]") : [];
    [...(root.matches && root.matches("[data-css]") ? [root] : []), ...els].forEach((el) => {
      el.dataset.css.split(";").forEach((rule) => {
        const at = rule.indexOf(":");
        if (at > 0) el.style.setProperty(rule.slice(0, at).trim(), rule.slice(at + 1).trim());
      });
      el.removeAttribute("data-css");
    });
  };
  apply(document);
  new MutationObserver((changes) => changes.forEach((c) => c.addedNodes.forEach(apply)))
    .observe(document.documentElement, {childList: true, subtree: true});
})();
