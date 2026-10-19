"use client";
// A virtual list: only the rows in view (plus a few either side) exist in the page, so a playlist with
// thousands of videos stays fast. Rows have a fixed height.
import { forwardRef, useImperativeHandle, useLayoutEffect, useRef, useState } from "react";

export interface VirtualListApi { reveal: (key: string) => void }

interface Props<T> {
  items: T[];
  rowHeight: number;
  rowKey: (item: T) => string;
  renderRow: (item: T, top: number) => React.ReactNode;   // place the row with translateY(top px)
  className?: string;
  overscan?: number;
}

function VirtualListInner<T>({ items, rowHeight, rowKey, renderRow, className = "", overscan = 8 }: Props<T>,
  ref: React.ForwardedRef<VirtualListApi>) {
  const box = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(480);
  const frame = useRef(0);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setHeight(el.clientHeight || 480);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useImperativeHandle(ref, () => ({
    reveal(key) {
      const i = items.findIndex((it) => rowKey(it) === key);
      if (i >= 0 && box.current) box.current.scrollTop = Math.max(0, i * rowHeight - box.current.clientHeight / 3);
    },
  }), [items, rowHeight, rowKey]);

  const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const last = Math.min(items.length, Math.ceil((scrollTop + height) / rowHeight) + overscan);
  const rows: React.ReactNode[] = [];
  for (let i = first; i < last; i++) rows.push(renderRow(items[i], i * rowHeight));

  return (
    <div className={className} ref={box} onScroll={(e) => {
      const top = e.currentTarget.scrollTop;
      cancelAnimationFrame(frame.current);
      frame.current = requestAnimationFrame(() => setScrollTop(top));
    }}>
      <div className="vl-inner" style={{ height: items.length * rowHeight }}>{rows}</div>
    </div>
  );
}

export const VirtualList = forwardRef(VirtualListInner) as <T>(p: Props<T> & { ref?: React.Ref<VirtualListApi> }) => React.ReactElement;
