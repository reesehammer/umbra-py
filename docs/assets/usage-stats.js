// Live usage panel for the docs home page. A failed fetch stays on the page
// as one quiet line and never throws into the rest of the document.
(function () {
  var ENDPOINT = "https://api.umbra-py.space/stats";
  var TIMEOUT_MS = 4000;

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

  function render(root, data) {
    if (!data || typeof data.searches_served !== "number" || typeof data.counting_since !== "string") {
      fail(root);
      return;
    }
    root.hidden = false;
    root.textContent = "";
    root.style.margin = "1em 0 1.25em";
    var since = data.counting_since.slice(0, 10);
    var lead = document.createElement("p");
    lead.style.margin = "0.2em 0";
    lead.appendChild(document.createTextNode("Searches served since " + since + ": "));
    var strong = document.createElement("strong");
    strong.textContent = formatNumber(data.searches_served);
    lead.appendChild(strong);
    root.appendChild(lead);

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
    heading.textContent = "Top places searched (3+ distinct clients in a day)";
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
