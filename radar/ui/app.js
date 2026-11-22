/* WA Council Works Radar UI: vanilla JS, no build step. Data comes from data.js (window.RADAR_DATA).
   All text is inserted with textContent, and links are limited to http(s), because the data comes from public documents. */
(function () {
  "use strict";
  var DATA = window.RADAR_DATA || { projects: [], councils: [], trades: [] };
  var MAX_ROWS = 1000;
  var $ = function (id) { return document.getElementById(id); };
  var councilName = {}, tradeLabel = {};
  DATA.councils.forEach(function (c) { councilName[c.id] = c.name; });
  DATA.trades.forEach(function (t) { tradeLabel[t.code] = t.label; });
  DATA.projects.forEach(function (p) { p._text = [p.name, p.road, p.from, p.to, p.suburb, p.code].join(" ").toLowerCase(); });

  var state = { sort: "value", dir: "desc", expanded: {} };
  var money = function (v) { return v == null ? "—" : "$" + Math.round(v).toLocaleString("en-AU"); };
  var confLabel = function (c) { return c >= 0.75 ? "High" : c >= 0.5 ? "Medium" : "Low"; };
  var safeUrl = function (u) { return /^https?:\/\//i.test(u || "") ? u : null; };

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k]; else if (k === "class") n.className = attrs[k]; else n.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  function checkbox(group, value, label, onChange) {
    var input = el("input", { type: "checkbox", value: value, "data-group": group });
    input.addEventListener("change", onChange);
    return el("label", { class: "inline" }, [input, document.createTextNode(" " + label)]);
  }
  function checked(containerId) {
    return Array.prototype.slice.call($(containerId).querySelectorAll("input[data-group]:checked")).map(function (i) { return i.value; });
  }
  function distinct(key) {
    var seen = {};
    DATA.projects.forEach(function (p) { if (p[key]) seen[p[key]] = 1; });
    return Object.keys(seen).sort();
  }

  /* ---- build the controls ---- */
  DATA.councils.forEach(function (c) { $("filter-council").appendChild(checkbox("council", c.id, c.name, apply)); });
  var present = {};
  DATA.projects.forEach(function (p) { present[p.trade] = 1; (p.trades || []).concat(p.implied || []).forEach(function (t) { present[t] = 1; }); });
  DATA.trades.filter(function (t) { return present[t.code]; }).forEach(function (t) {
    $("filter-trade").appendChild(checkbox("trade", t.code, t.label, apply));
  });
  distinct("stage").forEach(function (s) { $("filter-stage").appendChild(el("option", { value: s, text: s })); });
  distinct("fy").forEach(function (s) { $("filter-fy").appendChild(el("option", { value: s, text: s })); });
  $("meta").textContent = "Week " + (DATA.iso_week || "") + (DATA.baseline ? " · baseline run" : "") + " · data generated " + (DATA.generated_at || "");
  $("footer-text").textContent = DATA.footer || "";
  $("footer-note").textContent = [DATA.noncommercial, DATA.blackspot_note].filter(Boolean).join(" ");

  /* ---- filter state <-> URL hash ---- */
  var FIELDS = ["search", "filter-stage", "filter-fy", "value-min", "value-max", "filter-confidence", "filter-weeks"];
  function writeHash() {
    var q = new URLSearchParams();
    FIELDS.forEach(function (id) { if ($(id).value && $(id).value !== "0") q.set(id, $(id).value); });
    ["council", "trade"].forEach(function (g) { var v = checked("filter-" + g); if (v.length) q.set(g, v.join(",")); });
    if ($("include-implied").checked) q.set("implied", "1");
    if ($("filter-flags").checked) q.set("flags", "1");
    if (state.sort !== "value" || state.dir !== "desc") q.set("sort", state.sort + ":" + state.dir);
    var h = q.toString();
    if (("#" + h) !== location.hash && (h || location.hash)) history.replaceState(null, "", h ? "#" + h : location.pathname + location.search);
  }
  function readHash() {
    var q = new URLSearchParams(location.hash.replace(/^#/, ""));
    FIELDS.forEach(function (id) { $(id).value = q.get(id) || (id === "filter-confidence" ? "0" : ""); });
    ["council", "trade"].forEach(function (g) {
      var want = (q.get(g) || "").split(",");
      Array.prototype.forEach.call($("filter-" + g).querySelectorAll("input[data-group]"), function (i) { i.checked = want.indexOf(i.value) >= 0; });
    });
    $("include-implied").checked = q.get("implied") === "1";
    $("filter-flags").checked = q.get("flags") === "1";
    var s = (q.get("sort") || "value:desc").split(":");
    state.sort = s[0]; state.dir = s[1] === "asc" ? "asc" : "desc";
  }

  /* ---- filtering + sorting ---- */
  function lastChangeMs(p) { return p.last_change && p.last_change.at ? Date.parse(p.last_change.at) : 0; }
  function matches(p) {
    var councils = checked("filter-council"), trades = checked("filter-trade");
    if (councils.length && councils.indexOf(p.council) < 0) return false;
    if (trades.length) {
      var have = (p.trades || []).concat($("include-implied").checked ? (p.implied || []) : []);
      if (!trades.some(function (t) { return have.indexOf(t) >= 0; })) return false;
    }
    if ($("filter-stage").value && p.stage !== $("filter-stage").value) return false;
    if ($("filter-fy").value && p.fy !== $("filter-fy").value) return false;
    var min = parseFloat($("value-min").value), max = parseFloat($("value-max").value);
    if (!isNaN(min) && (p.value == null || p.value < min)) return false;
    if (!isNaN(max) && (p.value == null || p.value > max)) return false;
    if (p.confidence < parseFloat($("filter-confidence").value || "0")) return false;
    var weeks = parseFloat($("filter-weeks").value);
    if (!isNaN(weeks) && lastChangeMs(p) < Date.now() - weeks * 7 * 864e5) return false;
    if ($("filter-flags").checked && !(p.flags && p.flags.length)) return false;
    var term = $("search").value.trim().toLowerCase();
    return !term || term.split(/\s+/).every(function (w) { return p._text.indexOf(w) >= 0; });
  }
  function sortKey(p) {
    switch (state.sort) {
      case "council": return councilName[p.council] || p.council;
      case "sources": return p.sources.length;
      case "last_change": return lastChangeMs(p);
      case "trade": return p.trade || "";
      default: return p[state.sort] == null ? (typeof p[state.sort] === "number" ? -1 : "") : p[state.sort];
    }
  }
  function sorted(list) {
    var sign = state.dir === "asc" ? 1 : -1;
    return list.slice().sort(function (a, b) {
      var x = sortKey(a), y = sortKey(b);
      if (typeof x === "string") x = x.toLowerCase();
      if (typeof y === "string") y = y.toLowerCase();
      return (x < y ? -1 : x > y ? 1 : 0) * sign || (a.name || "").localeCompare(b.name || "");
    });
  }

  /* ---- rendering ---- */
  function chips(p) {
    var out = [];
    (p.trades || []).forEach(function (t) { out.push(el("span", { class: "chip", text: tradeLabel[t] ? t : t, title: tradeLabel[t] || t })); });
    (p.implied || []).forEach(function (t) { out.push(el("span", { class: "chip implied", text: t, title: "implied: " + (tradeLabel[t] || t) })); });
    return out;
  }
  function pills(flags) { return (flags || []).map(function (f) { return el("span", { class: "pill", text: f.replace(/_/g, " ") }); }); }
  function detail(p) {
    var box = el("div", {});
    var where = [p.road, p.from && p.to ? p.from + " → " + p.to : null, p.suburb].filter(Boolean).join(" · ");
    if (where) box.appendChild(el("p", { class: "small muted", text: "Location: " + where }));
    if (p.funding && p.funding.length) box.appendChild(el("p", { class: "small", text: "Funding: " + p.funding.join(" / ") }));
    if (p.flags && p.flags.length) box.appendChild(el("p", {}, pills(p.flags)));
    box.appendChild(el("h4", { text: "Sources (" + p.sources.length + ")" }));
    p.sources.forEach(function (s) {
      var url = safeUrl(s.url);
      var label = (s.title || s.doc_type) + ", p. " + s.page + ", retrieved " + (s.retrieved_at || "").slice(0, 10);
      var line = el("div", { class: "src" }, [
        el("div", {}, [url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: label }) : el("span", { text: label })]),
        el("div", { class: "small muted", text: [s.name, s.value != null ? money(s.value) + (s.value_basis ? " (" + s.value_basis + ")" : "") : null,
          s.stage, "extractor " + (s.extractor || "?") + (s.extractor_version ? " " + s.extractor_version : "")].filter(Boolean).join(" · ") }),
        s.quote ? el("div", { class: "quote", text: "“" + s.quote + "”" }) : null,
        s.flags && s.flags.length ? el("div", {}, pills(s.flags)) : null
      ]);
      box.appendChild(line);
    });
    if (p.history && p.history.length) {
      box.appendChild(el("h4", { text: "Change history" }));
      var ul = el("ul", { class: "timeline" });
      p.history.forEach(function (h) {
        ul.appendChild(el("li", { text: (h.at || "").slice(0, 10) + "  " + h.type + (h.new && h.new.value != null ? " → " + money(h.new.value) : h.new && h.new.stage ? " → " + h.new.stage : "") }));
      });
      box.appendChild(ul);
    }
    return box;
  }
  function render() {
    var list = sorted(DATA.projects.filter(matches));
    var total = list.reduce(function (s, p) { return s + (p.value || 0); }, 0);
    $("count-shown").textContent = list.length;
    $("count-total").textContent = DATA.projects.length;
    $("value-total").textContent = money(total);
    $("empty").hidden = list.length > 0;
    $("truncated").hidden = list.length <= MAX_ROWS;
    $("truncated").textContent = "Showing the first " + MAX_ROWS + " of " + list.length + " projects; narrow the filters to see the rest (the CSV has all of them).";
    var body = $("results-body");
    body.textContent = "";
    list.slice(0, MAX_ROWS).forEach(function (p) {
      var open = !!state.expanded[p.id];
      var row = el("tr", { class: "proj", "data-id": p.id, "aria-expanded": String(open) }, [
        el("td", { text: p.name || "(unnamed)" }), el("td", { text: councilName[p.council] || p.council }), el("td", { text: p.suburb || "" }),
        el("td", {}, chips(p)), el("td", { class: "num", text: money(p.value) }), el("td", { text: (p.value_basis || "").replace(/_/g, " ") }),
        el("td", { text: p.stage || "" }), el("td", { text: p.last_change ? p.last_change.type + " " + (p.last_change.at || "").slice(0, 10) : "" }),
        el("td", {}, [el("span", { class: "conf-" + confLabel(p.confidence), text: confLabel(p.confidence) + " (" + p.confidence.toFixed(2) + ")" })]),
        el("td", { class: "num", text: String(p.sources.length) })
      ]);
      row.addEventListener("click", function () { state.expanded[p.id] = !state.expanded[p.id]; render(); });
      body.appendChild(row);
      if (open) body.appendChild(el("tr", { class: "detail" }, [el("td", { colspan: "10" }, [detail(p)])]));
    });
    Array.prototype.forEach.call($("results-head").children, function (th) {
      th.className = (th.className || "").replace(/\bsorted-(asc|desc)\b/g, "").trim() + (th.dataset.sort === state.sort ? " sorted-" + state.dir : "");
    });
    return list;
  }
  function apply() { writeHash(); render(); }

  /* ---- CSV (client side, current filter) ---- */
  function csvCell(v) {
    var s = v == null ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;            // keep spreadsheet apps from evaluating text as formulas
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function csv(list) {
    var head = ["project", "council", "suburb", "road", "trades", "implied_trades", "value", "value_basis", "stage", "financial_year", "confidence", "flags", "last_change", "source_url", "source_page", "retrieved_at"];
    var rows = list.map(function (p) {
      var s = p.sources[0] || {};
      return [p.name, councilName[p.council] || p.council, p.suburb, p.road, (p.trades || []).join("|"), (p.implied || []).join("|"), p.value, p.value_basis,
        p.stage, p.fy, p.confidence, (p.flags || []).join("|"), p.last_change ? p.last_change.type + " " + p.last_change.at : "", s.url, s.page, s.retrieved_at];
    });
    return [head].concat(rows).map(function (r) { return r.map(csvCell).join(","); }).join("\r\n");
  }
  $("download-csv").addEventListener("click", function () {
    var list = sorted(DATA.projects.filter(matches));
    var url = URL.createObjectURL(new Blob([csv(list)], { type: "text/csv" }));
    var a = el("a", { href: url, download: "radar-projects.csv" });
    document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
  });

  /* ---- wire up ---- */
  FIELDS.forEach(function (id) { $(id).addEventListener("input", apply); $(id).addEventListener("change", apply); });
  ["include-implied", "filter-flags"].forEach(function (id) { $(id).addEventListener("change", apply); });
  $("reset").addEventListener("click", function () { history.replaceState(null, "", location.pathname + location.search); readHash(); render(); });
  Array.prototype.forEach.call($("results-head").children, function (th) {
    th.addEventListener("click", function () {
      var key = th.dataset.sort;
      state.dir = state.sort === key && state.dir === "desc" ? "asc" : (state.sort === key ? "desc" : (key === "value" || key === "confidence" || key === "sources" || key === "last_change" ? "desc" : "asc"));
      state.sort = key; apply();
    });
  });
  window.addEventListener("hashchange", function () { readHash(); render(); });
  window.RADAR_UI = { matches: matches, csv: csv, render: render };      // handy for debugging in the console
  readHash(); render();
})();
