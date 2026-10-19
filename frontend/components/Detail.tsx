"use client";
// The facts about one video's download: format, folder, file, attempts and its log lines. Shown in the
// Details dialog and inside expanded dashboard rows.
import { useLayoutEffect, useRef } from "react";
import { AUDIO, fileName, fmtAgo, fmtTime } from "../lib/util";

const QUALITY = (q: string) => (q === "best" ? "Best available" : /^\d+$/.test(q) ? `${q}p` : String(q || "").toUpperCase());
export const KIND: Record<string, string> = { network: "Connection", ratelimit: "Rate limit", permanent: "Can't download", disk: "Disk", other: "Error" };

export function Detail({ d }: { d: any }) {
  const o = d.options || {};
  const chosen = [QUALITY(o.quality || "best"), AUDIO.includes(o.quality) ? "" : (o.container || "mp4").toUpperCase(),
    o.subtitles ? `subtitles ${o.sub_langs || "en"}${o.auto_subs ? " (+auto)" : ""}` : "",
    o.sponsorblock ? `SponsorBlock: ${o.sponsorblock}` : "",
    o.section ? `part ${fmtTime(o.section.start)}–${o.section.end != null ? fmtTime(o.section.end) : "end"}` : ""].filter(Boolean).join(" · ");
  const rows: [string, string][] = [
    ["Chosen", chosen],
    ["Got", d.fmt || "—"],
    ["Folder", d.outdir],
    ["File", d.filepath ? fileName(d.filepath) : "—"],
  ];
  if (d.step) rows.push(["Now", d.step]);
  if (d.started) rows.push(["Started", `${new Date(d.started * 1000).toLocaleString()} (${fmtAgo(d.started)})`]);
  const tries = (d.history || []).slice().reverse();
  const text = d.lines?.length ? d.lines.join("\n") : "No messages recorded for this video.";

  // keep the log scrolled to the end while it grows, unless the user scrolled up
  const pre = useRef<HTMLPreElement>(null);
  const atEnd = useRef(true);
  useLayoutEffect(() => {
    if (pre.current && atEnd.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [text]);

  return (
    <>
      <dl className="facts">
        {rows.map(([k, v]) => <FactRow key={k} k={k} v={v} />)}
      </dl>
      {tries.length > 0 && <>
        <div className="sub-h">Attempts</div>
        <ol className="tries">
          {tries.map((t: any, i: number) => (
            <li key={i}><span className={`kind k-${t.kind}`}>{KIND[t.kind] || t.kind}</span>
              <span className="when">{new Date(t.t * 1000).toLocaleTimeString()}</span><span className="msg">{t.msg}</span></li>
          ))}
        </ol>
      </>}
      <div className="sub-h">Log</div>
      <pre className="log-lines" ref={pre}
        onScroll={(e) => { const p = e.currentTarget; atEnd.current = p.scrollTop + p.clientHeight >= p.scrollHeight - 4; }}>
        {text}
      </pre>
    </>
  );
}

const FactRow = ({ k, v }: { k: string; v: string }) => <><dt>{k}</dt><dd title={v}>{v}</dd></>;
