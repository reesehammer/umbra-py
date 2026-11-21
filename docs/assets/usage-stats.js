// Live usage panel for the docs home page. A failed fetch stays on the page
// as one quiet line and never throws into the rest of the document.
(function () {
  var ENDPOINT = "https://api.umbra-py.space/stats";
  var TIMEOUT_MS = 4000;
  var FALLBACK_SINCE = "Nov 20, 2026";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function boot() {
    var root = document.getElementById("umbra-usage");
    if (!root || root.getAttribute("data-umbra-usage") === "1") return;
    root.setAttribute("data-umbra-usage", "1");
    load(root);
  }

  function load(root) {
    var ctrl = typeof AbortController === "function" ? new AbortController() : null;
    var timer = setTimeout(function () {
      if (ctrl) ctrl.abort();
      else fail(root);
    }, TIMEOUT_MS);
    var opts = {};
    if (ctrl) opts.signal = ctrl.signal;
    fetch(ENDPOINT, opts)
      .then(function (response) {
        if (!response.ok) throw new Error("status");
        return response.json();
      })
      .then(function (data) {
        clearTimeout(timer);
        render(root, data);
      })
      .catch(function () {
        clearTimeout(timer);
        fail(root);
      });
  }

  function fail(root) {
    root.hidden = false;
    root.textContent = "";
    var note = document.createElement("p");
    note.textContent = "stats unavailable";
    note.style.margin = "0.5em 0";
    note.style.opacity = "0.65";
    note.style.fontSize = "0.85em";
    root.appendChild(note);
  }

  function sinceParts(value) {
    if (typeof value !== "string") return null;
    var match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
    if (!match) return null;
    var month = MONTHS[Number(match[2]) - 1];
    var day = Number(match[3]);
    if (!month || day < 1 || day > 31) return null;
    return {
      iso: match[1] + "-" + match[2] + "-" + match[3],
      label: month + " " + day + ", " + match[1],
    };
  }

  function render(root, data) {
    if (!data || typeof data.searches_served !== "number") {
      fail(root);
      return;
    }
    root.hidden = false;
    root.style.margin = "1em 0 1.25em";
    var parts = sinceParts(data.counting_since);
    var since = parts ? parts.label : FALLBACK_SINCE;
    var lead = document.getElementById("umbra-usage-lead");
    if (!lead || lead.parentNode !== root) {
      root.textContent = "";
      lead = document.createElement("p");
      lead.id = "umbra-usage-lead";
      lead.appendChild(document.createTextNode("Searches served since "));
      var time = document.createElement("time");
      time.id = "umbra-usage-since";
      lead.appendChild(time);
      lead.appendChild(document.createTextNode(": "));
      var strong = document.createElement("strong");
      strong.id = "umbra-usage-count";
      lead.appendChild(strong);
      root.appendChild(lead);
    }
    lead.style.margin = "0.2em 0";
    var sinceEl = document.getElementById("umbra-usage-since");
    var countEl = document.getElementById("umbra-usage-count");
    if (sinceEl) {
      sinceEl.textContent = since;
      if (typeof sinceEl.setAttribute === "function") {
        sinceEl.setAttribute("datetime", parts ? parts.iso : "2026-11-20");
      }
    }
    if (countEl) countEl.textContent = formatNumber(data.searches_served);
    while (lead.nextSibling) root.removeChild(lead.nextSibling);

    var bits = [];
    if (typeof data.scenes_indexed === "number") {
      bits.push(formatNumber(data.scenes_indexed) + " scenes indexed");
    }
    if (typeof data.thumbnails === "number") {
      bits.push(formatNumber(data.thumbnails) + " previews");
    }
    if (typeof data.index_built_at === "string" && data.index_built_at) {
      bits.push("index built " + data.index_built_at);
    }
    if (bits.length) {
      var meta = document.createElement("p");
      meta.textContent = bits.join(" · ");
      meta.style.margin = "0.2em 0";
      meta.style.opacity = "0.8";
      meta.style.fontSize = "0.85em";
      root.appendChild(meta);
    }

    var places = Array.isArray(data.top_places) ? data.top_places : [];
    var named = places.filter(function (row) {
      return row && typeof row.place === "string" && typeof row.clients === "number" && row.clients >= 3;
    });
    if (!named.length) return;
    var heading = document.createElement("p");
    heading.textContent = "Top places searched since " + since + " (3+ distinct clients in a day)";
    heading.style.margin = "0.6em 0 0.2em";
    heading.style.fontSize = "0.85em";
    root.appendChild(heading);
    var list = document.createElement("ol");
    list.style.margin = "0.2em 0 0";
    list.style.paddingLeft = "1.2em";
    named.slice(0, 10).forEach(function (row) {
      var item = document.createElement("li");
      item.textContent = row.place + " (" + formatNumber(row.clients) + ")";
      list.appendChild(item);
    });
    root.appendChild(list);
  }

  function formatNumber(value) {
    try {
      return Number(value).toLocaleString("en-US");
    } catch (err) {
      return String(value);
    }
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(boot);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
