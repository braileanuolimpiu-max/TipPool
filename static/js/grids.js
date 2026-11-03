/* TipPool: interactive tables with AG Grid Community (MIT), for Reports, Past shifts and the Activity log.
   The page already shows a plain table (it works without JavaScript); this swaps it for a grid built from the
   same rows (tips/grids.py, in a json_script). Sort any column, filter, search, see totals for what's shown,
   and click a shift to open it. Text always goes in as text, never as HTML; links only to our own pages. */
(() => {
  const AG = window.agGrid;
  const wraps = document.querySelectorAll("[data-grid]");
  if (!AG || !wraps.length) return;

  // the page's nonce, so the grid's own styles pass the Content-Security-Policy (no 'unsafe-inline')
  const nonce = (document.querySelector("script[nonce]") || {}).nonce || "";
  const NARROW = 640;  // phones: the less important columns step aside

  // TipPool's own colors, as CSS variables: dark mode switches the grid with the rest of the page
  const theme = AG.themeQuartz.withParams({
    backgroundColor: "var(--paper)", foregroundColor: "var(--ink)", borderColor: "var(--hair)",
    headerBackgroundColor: "var(--steel-1)", headerTextColor: "var(--graphite)", headerFontWeight: 700,
    accentColor: "var(--ink)", rowHoverColor: "var(--marker-soft)", selectedRowBackgroundColor: "var(--marker-soft)",
    oddRowBackgroundColor: "var(--paper)", fontFamily: "inherit", fontSize: 15, headerFontSize: 13,
    spacing: 7, wrapperBorder: false, wrapperBorderRadius: 0, rowBorder: true, columnBorder: false,
    inputBorder: "1.5px solid var(--hair)", inputFocusBorder: "1.5px solid var(--ink)",
  });

  const nf2 = new Intl.NumberFormat("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const nfHours = new Intl.NumberFormat("en-US", {maximumFractionDigits: 2});
  const money = (cur) => (p) => p.value == null ? "–" : cur + nf2.format(p.value);
  const hours = (p) => p.value == null ? "" : nfHours.format(p.value);
  const ownPath = (url) => typeof url === "string" && url.startsWith("/") && !url.startsWith("//") ? url : null;

  function linkCell(p) {
    const url = p.data && ownPath(p.data.url);
    if (!url) return "";
    const a = document.createElement("a");
    a.href = url;
    a.textContent = p.data.action;
    return a;
  }

  function pillCell(p) {
    const span = document.createElement("span");
    const kind = {ok: "ok", wait: "wait", live: "live"}[p.data.statusKey] || "";
    span.className = "pill" + (kind ? " " + kind : "");
    span.textContent = p.value;
    return span;
  }

  const num = (field, headerName, extra = {}) => ({field, headerName, type: "rightAligned", filter: "agNumberColumnFilter",
                                                   minWidth: 96, ...extra});
  const text = (field, headerName, extra = {}) => ({field, headerName, filter: "agTextColumnFilter", minWidth: 120, ...extra});
  // a shift's date sorts by the real date and shows "Mon, Nov 2"
  const shiftDate = {field: "date", headerName: "Shift", minWidth: 120, sort: "desc", filter: "agTextColumnFilter",
                     valueFormatter: (p) => p.data ? p.data.label : p.value,
                     filterValueGetter: (p) => p.data.label};

  function totals(rows, kind) {
    const sum = (key) => rows.reduce((t, r) => t + (r[key] || 0), 0);
    if (kind === "people") {
      const h = sum("hours"), pool = sum("pool");
      return {name: "Total", roles: "", shifts: sum("shifts"), hours: h, pool, personal: sum("personal") || null,
              total: sum("total"), perHour: h ? pool / h : null};
    }
    return {date: "", label: "Total", total: sum("total"), people: null};
  }

  function columnsFor(kind, cur, rows) {
    const m = money(cur);
    if (kind === "people") return [
      text("name", "Name", {minWidth: 140, flex: 1.3, pinned: "left", cellClass: "grid-strong"}),
      text("roles", "Role", {flex: 1, narrow: false}),
      num("shifts", "Shifts", {narrow: false}),
      num("hours", "Hours", {valueFormatter: hours}),
      num("pool", "Shared", {valueFormatter: m, narrow: false}),
      num("personal", "Personal", {valueFormatter: m, narrow: false}),
      num("total", "Total", {valueFormatter: m, sort: "desc", cellClass: "grid-strong"}),
      num("perHour", "Shared / hour", {valueFormatter: m, minWidth: 124, narrow: false}),
    ];
    if (kind === "shifts") return [
      shiftDate,
      ...(rows.some((r) => r.location) ? [text("location", "Location", {flex: 1, narrow: false})] : []),
      num("total", "Tips", {valueFormatter: m}),
      num("people", "People", {narrow: false}),
      text("status", "Status", {flex: 1, minWidth: 110, cellClass: "muted", wrapText: true, autoHeight: true}),
      {field: "action", headerName: "", sortable: false, filter: false, minWidth: 130, cellRenderer: linkCell, narrow: false},
    ];
    if (kind === "history") return [
      shiftDate,
      num("total", "Tips", {valueFormatter: m}),
      num("people", "People paid", {minWidth: 116, narrow: false}),
      text("status", "Status", {minWidth: 150, flex: 1, cellRenderer: pillCell, wrapText: true, autoHeight: true}),
      {field: "action", headerName: "", sortable: false, filter: false, minWidth: 140, cellRenderer: linkCell, narrow: false},
    ];
    // the activity log
    return [
      {field: "at", headerName: "When", minWidth: 136, sort: "desc", valueFormatter: (p) => p.data.when,
       filter: "agTextColumnFilter", filterValueGetter: (p) => p.data.when, cellClass: "grid-mono"},
      ...(rows.some((r) => "location" in r) ? [text("location", "Location", {narrow: false})] : []),
      text("who", "Who", {minWidth: 130, narrow: false}),
      text("what", "What", {flex: 3, minWidth: 220, wrapText: true, autoHeight: true}),
    ];
  }

  wraps.forEach((wrap) => {
    const kind = wrap.dataset.grid;
    const source = document.getElementById(wrap.dataset.gridRows);
    let rows;
    try { rows = JSON.parse(source.textContent); } catch (e) { return; }  // keep the plain table
    if (!Array.isArray(rows) || !rows.length) return;
    const cur = wrap.dataset.cur || "";
    const linked = kind === "shifts" || kind === "history";
    // phones get fewer columns that share the width (nothing pinned, no fixed widths), so nothing is cut off
    const defsFor = (narrow) => columnsFor(kind, cur, rows).filter((c) => !(narrow && c.narrow === false)).map((c) => {
      const d = {...c};
      delete d.narrow;
      if (narrow) {
        delete d.pinned; d.flex = d.flex || 1;
        d.minWidth = Math.min(d.minWidth || 80, d.field === "name" || d.field === "what" ? 110 : 78);
        if (d.field === "date" || d.field === "at") {
          d.flex = 0; d.width = d.minWidth = d.field === "at" ? 122 : 86;
          d.valueFormatter = (p) => p.data ? (p.data.short || p.data.label || p.data.when) : p.value;
        }
      }
      return d;
    });
    const colDefaults = (narrow) => ({sortable: true, resizable: !narrow, suppressHeaderFilterButton: narrow,
                                      filterParams: {buttons: ["reset"]}});
    let narrowNow = window.innerWidth <= NARROW;

    // a search box over the grid (the visible rows only; nothing is sent anywhere)
    const tools = document.createElement("div");
    tools.className = "grid-tools";
    const search = document.createElement("input");
    search.type = "search";
    search.className = "grid-search";
    search.placeholder = wrap.dataset.searchLabel || "Search this table";
    search.setAttribute("aria-label", search.placeholder);
    const count = document.createElement("span");
    count.className = "small muted grid-count";
    count.setAttribute("aria-live", "polite");
    tools.append(search, count);

    const box = document.createElement("div");
    box.className = "tp-grid" + (rows.length > 60 ? " tall" : "");
    box.setAttribute("aria-label", wrap.dataset.gridLabel || "Table");
    wrap.after(tools, box);

    const showTotals = kind === "people" || kind === "shifts";
    const refresh = (api) => {
      const shown = [];
      api.forEachNodeAfterFilterAndSort((n) => shown.push(n.data));
      if (showTotals) api.setGridOption("pinnedBottomRowData", shown.length ? [totals(shown, kind)] : []);
      count.textContent = shown.length === rows.length ? `${rows.length} row${rows.length === 1 ? "" : "s"}`
                                                       : `${shown.length} of ${rows.length} rows`;
    };
    const fitWidth = (api) => {
      const narrow = window.innerWidth <= NARROW;
      if (narrow !== narrowNow) {
        narrowNow = narrow;
        api.setGridOption("defaultColDef", colDefaults(narrow));
        api.setGridOption("columnDefs", defsFor(narrow));
      }
      api.sizeColumnsToFit();
    };
    const open = (data) => { const url = data && ownPath(data.url); if (url) window.location.href = url; };

    const api = AG.createGrid(box, {
      theme, styleNonce: nonce, rowData: rows, columnDefs: defsFor(narrowNow), defaultColDef: colDefaults(narrowNow),
      domLayout: rows.length > 60 ? "normal" : "autoHeight",
      ensureDomOrder: true, enableCellTextSelection: true, suppressMovableColumns: narrowNow,
      animateRows: false, rowHeight: kind === "activity" ? undefined : 46,
      getRowClass: (p) => linked && !p.node.rowPinned ? "grid-row-link" : (p.node.rowPinned ? "grid-total" : ""),
      localeText: {noRowsToShow: "Nothing matches"},
      onRowClicked: (e) => {
        if (!linked || e.node.rowPinned || (e.event && e.event.target.closest("a"))) return;
        if (window.getSelection && String(window.getSelection())) return;  // they were selecting text to copy
        open(e.data);
      },
      onCellKeyDown: (e) => { if (linked && e.event.key === "Enter" && !e.node.rowPinned) open(e.data); },
      onFirstDataRendered: (e) => { fitWidth(e.api); refresh(e.api); },
      onGridSizeChanged: (e) => fitWidth(e.api),
      onFilterChanged: (e) => refresh(e.api),
      onSortChanged: (e) => refresh(e.api),
    });
    search.addEventListener("input", () => api.setGridOption("quickFilterText", search.value));
    wrap.hidden = true;  // the grid has the same rows; the plain table stays for printing and no-JavaScript
  });
})();
