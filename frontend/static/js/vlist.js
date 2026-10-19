// A virtual list: only the rows in view (plus a few either side) exist in the page, so a playlist with
// thousands of videos stays fast. Rows have a fixed height.

export class VList {
  /**
   * @param {HTMLElement} box   the scrolling container
   * @param {object} o          { rowHeight, key(item), html(item), tag, className, decorate(el, item) }
   */
  constructor(box, o) {
    this.box = box;
    this.o = { overscan: 8, tag: "div", className: "", ...o };
    this.items = [];
    this.rows = new Map();               // key -> { el, html }
    this.inner = document.createElement("div");
    this.inner.className = "vl-inner";
    box.appendChild(this.inner);
    let queued = false;
    box.addEventListener("scroll", () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; this.draw(); });
    }, { passive: true });
  }

  setItems(items) {
    this.items = items;
    this.inner.style.height = `${items.length * this.o.rowHeight}px`;
    this.draw();
  }

  /** Render the rows that are in view; rows whose HTML didn't change aren't touched. */
  draw() {
    const h = this.o.rowHeight, n = this.items.length;
    const view = this.box.clientHeight || 480;
    const first = Math.max(0, Math.floor(this.box.scrollTop / h) - this.o.overscan);
    const last = Math.min(n, Math.ceil((this.box.scrollTop + view) / h) + this.o.overscan);
    const keep = new Set();
    for (let i = first; i < last; i++) {
      const item = this.items[i], key = this.o.key(item);
      keep.add(key);
      let row = this.rows.get(key);
      if (!row) {
        row = { el: document.createElement(this.o.tag), html: null, top: null };
        row.el.className = this.o.className;
        row.el.dataset.key = key;
        this.inner.appendChild(row.el);
        this.rows.set(key, row);
      }
      const html = this.o.html(item);
      if (row.html !== html) { row.el.innerHTML = row.html = html; }
      if (row.top !== i * h) { row.el.style.transform = `translateY(${(row.top = i * h)}px)`; }
      this.o.decorate?.(row.el, item);
    }
    for (const [key, row] of this.rows) if (!keep.has(key)) { row.el.remove(); this.rows.delete(key); }
  }

  /** Scroll so the item with this key is in view. */
  reveal(key) {
    const i = this.items.findIndex((it) => this.o.key(it) === key);
    if (i >= 0) this.box.scrollTop = Math.max(0, i * this.o.rowHeight - this.box.clientHeight / 3);
  }
}
