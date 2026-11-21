"""The docs home panel prints counting_since on the Pacific calendar.

The counter file is stamped in UTC. A launch at 2026-11-21T00:24:29Z is still
Nov 20 in America/Los_Angeles, and that is the date the page shows.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PANEL = REPO / "docs" / "assets" / "usage-stats.js"

_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(process.argv[2]);

class El {
  constructor(tag, text) {
    this.tag = tag;
    this.children = [];
    this.parentNode = null;
    this.attrs = {};
    this.style = {};
    this.hidden = false;
    this._text = text || "";
    this._id = "";
  }
  set id(value) {
    this._id = value;
    this.attrs.id = value;
    if (this._track) this._track(this);
  }
  get id() { return this._id; }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  get textContent() {
    if (this.children.length) return this.children.map((child) => child.textContent).join("");
    return this._text;
  }
  setAttribute(key, value) {
    this.attrs[key] = String(value);
    if (key === "id") this._id = this.attrs[key];
  }
  getAttribute(key) {
    return Object.prototype.hasOwnProperty.call(this.attrs, key) ? this.attrs[key] : null;
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null;
  }
  get nextSibling() {
    if (!this.parentNode) return null;
    const index = this.parentNode.children.indexOf(this);
    return this.parentNode.children[index + 1] || null;
  }
}

function renderCase(spec) {
  const ids = {};
  function track(el) {
    if (el.attrs.id) ids[el.attrs.id] = el;
  }
  const document = {
    getElementById(id) { return ids[id] || null; },
    createElement(tag) {
      const el = new El(tag);
      el._track = track;
      return el;
    },
    createTextNode(text) { return new El("#text", text); },
    readyState: "complete",
    addEventListener() {},
  };
  const root = new El("div");
  root.attrs.id = "umbra-usage";
  root.hidden = true;
  ids["umbra-usage"] = root;
  const lead = document.createElement("p");
  lead.id = "umbra-usage-lead";
  lead.appendChild(document.createTextNode("Searches served since "));
  const time = document.createElement("time");
  time.id = "umbra-usage-since";
  time.textContent = "Nov 20, 2026";
  time.setAttribute("datetime", "2026-11-20");
  lead.appendChild(time);
  lead.appendChild(document.createTextNode(": "));
  const count = document.createElement("strong");
  count.id = "umbra-usage-count";
  lead.appendChild(count);
  root.appendChild(lead);

  const sandbox = {
    document,
    window: {},
    fetch: async () => ({ ok: true, json: async () => spec.payload }),
    AbortController: class { abort() {} },
    setTimeout() { return 1; },
    clearTimeout() {},
    Date,
    console,
  };
  sandbox.Intl = spec.intl === false ? undefined : Intl;
  vm.runInNewContext(src, sandbox);
  return new Promise((resolve) => {
    setImmediate(() => {
      const since = ids["umbra-usage-since"];
      const heading = root.children
        .map((child) => child.textContent)
        .find((text) => text.indexOf("Top places searched since ") === 0) || null;
      resolve({
        since: since ? since.textContent : null,
        datetime: since ? since.getAttribute("datetime") : null,
        count: ids["umbra-usage-count"] ? ids["umbra-usage-count"].textContent : null,
        heading,
      });
    });
  });
}

(async () => {
  const rendered = [];
  for (const spec of cases) rendered.push(await renderCase(spec));
  process.stdout.write(JSON.stringify(rendered));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""


def _places() -> list[dict[str, object]]:
    return [{"place": "Austin, United States", "clients": 3}]


def _render(cases: list[dict[str, object]]) -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to render docs/assets/usage-stats.js")
    completed = subprocess.run(
        [node, "-e", _HARNESS, str(PANEL), json.dumps(cases)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    rendered = json.loads(completed.stdout)
    assert isinstance(rendered, list)
    return rendered


def test_launch_timestamp_reads_as_the_pacific_date():
    rendered = _render(
        [
            {
                "payload": {
                    "searches_served": 4,
                    "counting_since": "2026-11-21T00:24:29Z",
                    "top_places": _places(),
                }
            },
            {
                "payload": {
                    "searches_served": 4,
                    "counting_since": "2026-11-21T08:00:00Z",
                    "top_places": _places(),
                }
            },
        ]
    )
    launch, later = rendered
    assert launch["since"] == "Nov 20, 2026"
    assert launch["datetime"] == "2026-11-20"
    assert launch["count"] == "4"
    assert launch["heading"] == (
        "Top places searched since Nov 20, 2026 (3+ distinct clients in a day)"
    )
    assert later["since"] == "Nov 21, 2026"
    assert later["heading"] == (
        "Top places searched since Nov 21, 2026 (3+ distinct clients in a day)"
    )


def test_panel_keeps_the_static_date_when_the_timestamp_cannot_be_formatted():
    rendered = _render(
        [
            {
                "payload": {
                    "searches_served": 2,
                    "counting_since": "not-a-date",
                    "top_places": _places(),
                }
            },
            {"payload": {"searches_served": 2, "top_places": _places()}, "intl": False},
        ]
    )
    for row in rendered:
        assert row["since"] == "Nov 20, 2026"
        assert row["datetime"] == "2026-11-20"
        assert row["count"] == "2"
        assert row["heading"] == (
            "Top places searched since Nov 20, 2026 (3+ distinct clients in a day)"
        )
