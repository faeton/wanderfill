"use strict";
/* Runs a rendered trips page without a browser: its own scripts, cut out of the page,
   against the few DOM calls they make. Usage: node trips_page_dom.js PAGE BODY, where
   BODY is the inside of a function of (open, logic, data) whose result is printed. The
   elements and the storage are those of review_page_dom.js. */

const fs = require("fs");
const vm = require("vm");

const [, , pagePath, bodyPath] = process.argv;
const html = fs.readFileSync(pagePath, "utf8");

const cut = (id) => {
  const m = new RegExp(`<script[^>]* id="${id}"[^>]*>([\\s\\S]*?)</script>`).exec(html);
  if (!m) throw new Error(`the page has no script ${id}`);
  return m[1];
};
const data = JSON.parse(cut("data"));

class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attrs = {};
    this.on = {};
    this.own = "";
    this.hidden = false;
    this.open = false;
    this.value = "";
    this.className = "";
    this.style = {};
    this.offsetHeight = 100;
    this.writes = 0;
  }
  get textContent() {
    return this.own + this.children.map((c) => c.textContent).join("");
  }
  set textContent(text) {
    this.writes += 1;
    this.children = [];
    this.own = String(text);
  }
  set innerHTML(_) {
    throw new Error("innerHTML");
  }
  set outerHTML(_) {
    throw new Error("outerHTML");
  }
  append(...nodes) {
    for (const n of nodes) {
      if (!(n instanceof El)) throw new Error("append takes elements here");
      this.children.push(n);
    }
  }
  replaceChildren(...nodes) {
    this.own = "";
    this.children = [];
    this.append(...nodes);
  }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }
  getAttribute(name) {
    return name in this.attrs ? this.attrs[name] : null;
  }
  addEventListener(type, f) {
    (this.on[type] = this.on[type] || []).push(f);
  }
  fire(type) {
    for (const f of this.on[type] || []) f({ type, target: this });
  }
  click() {
    this.fire("click");
    if (this.tag === "a") El.clicked.push(this);
  }
  focus() {}
  select() {}
  remove() {}
  all(test) {
    const found = test(this) ? [this] : [];
    return found.concat(...this.children.map((c) => c.all(test)));
  }
  cls(name) {
    return this.all((e) => e.className.split(" ").includes(name));
  }
}
El.clicked = [];

class Storage {
  constructor() {
    this.held = new Map();
    this.log = [];
  }
  getItem(k) {
    this.log.push(["getItem", k]);
    return this.held.has(k) ? this.held.get(k) : null;
  }
  setItem(k, v) {
    this.log.push(["setItem", k]);
    this.held.set(k, String(v));
  }
  removeItem(k) {
    this.log.push(["removeItem", k]);
    this.held.delete(k);
  }
}

/* One tab's way to a storage: `cut` is called on either side of every operation it does,
   and what another tab does in there happens in the middle of this one's press. */
class View {
  constructor(storage, cut) {
    for (const op of ["getItem", "setItem", "removeItem"]) {
      this[op] = (...args) => {
        cut(op, "before");
        const out = storage[op](...args);
        cut(op, "after");
        return out;
      };
    }
  }
}

/* A page opened with this storage: `null` is a browser that refuses even to name it. */
function open({ storage = new Storage(), clipboard = "ok", observer = true } = {}) {
  const byId = {};
  const order = [];
  const body = html.replace(/<(script|style)[\s\S]*?<\/\1>/g, "");
  for (const m of body.matchAll(/<(\w+)([^>]*) id="([\w-]+)"([^>]*)>/g)) {
    const e = new El(m[1]);
    e.hidden = /\shidden(\s|$)/.test(m[2] + " " + m[4]);
    byId[m[3]] = e;
    order.push(m[3]);
  }
  byId.data = new El("script");
  byId.data.textContent = cut("data");
  const listeners = {};
  const blobs = [];
  const revoked = [];
  const timers = [];
  const copied = [];
  El.clicked = [];
  const window = {
    addEventListener: (type, f) => (listeners[type] = listeners[type] || []).push(f),
  };
  const root = new El("html");
  let resized = null;
  let watched = null;
  if (observer) {
    window.ResizeObserver = class {
      constructor(f) {
        resized = f;
      }
      observe(e) {
        watched = e;
        resized();
      }
    };
  }
  Object.defineProperty(window, "localStorage", {
    get() {
      if (storage === null) throw new Error("storage is off");
      return storage;
    },
  });
  const box = {
    window,
    document: {
      createElement: (tag) => new El(tag),
      documentElement: root,
      getElementById: (id) => byId[id] || null,
    },
    navigator:
      clipboard === "absent"
        ? {}
        : typeof clipboard === "object"
        ? { clipboard }
        : {
            clipboard: {
              writeText: async (text) => {
                if (clipboard === "refused") throw new Error("no");
                copied.push(text);
              },
            },
          },
    Blob: class {
      constructor(parts, options) {
        this.text = parts.join("");
        this.type = options.type;
      }
    },
    URL: {
      createObjectURL: (b) => `blob:${blobs.push(b)}`,
      revokeObjectURL: (u) => revoked.push(u),
    },
    setTimeout: (f) => timers.push(f),
  };
  vm.createContext(box);
  vm.runInContext(cut("logic"), box);
  vm.runInContext(cut("page"), box);
  const list = byId.list;
  return {
    byId, order, blobs, revoked, timers, copied, storage, root, watched,
    resized: () => resized(),
    clicked: El.clicked,
    tell: (type, event) => (listeners[type] || []).forEach((f) => f(event)),
    runs: () => list.cls("run"),
    run: (title) => list.cls("run").find((r) => r.cls("name")[0].textContent.startsWith(title)),
    betweens: () => list.cls("between"),
    press: (box, word) => {
      const found = box.all((e) => e.tag === "button" && e.textContent === word);
      if (found.length !== 1) throw new Error(`${found.length} buttons ${word}`);
      found[0].click();
    },
    buttons: (box) => box.all((e) => e.tag === "button").map((e) => e.textContent),
    field: (box) => box.all((e) => e.tag === "textarea")[0] || null,
    type: (box, text) => {
      const field = box.all((e) => e.tag === "textarea")[0];
      field.value = text;
      field.fire("input");
    },
    pressed: (box = list) =>
      box.all((e) => e.getAttribute("aria-pressed") === "true").map((e) => e.textContent),
  };
}

const alone = vm.createContext({});
vm.runInContext(cut("logic"), alone); /* with no document and no window */
const logic = vm.runInContext("Trips", alone);

const AsyncFunction = (async () => {}).constructor;
new AsyncFunction("open", "logic", "data", "Storage", "View", fs.readFileSync(bodyPath, "utf8"))(
  open, logic, data, Storage, View,
).then(
  (result) => process.stdout.write(JSON.stringify(result)),
  (error) => {
    process.stderr.write(String(error && error.stack));
    process.exitCode = 1;
  },
);
