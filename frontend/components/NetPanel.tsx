"use client";
// The connection panel: is the internet / YouTube reachable, how fast it responds, and the history chart.
// Also the connection pill in the top bar.
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { fmtSpeed } from "../lib/util";
import { useApp } from "./AppContext";
import { LineChart } from "./charts";

export const OFFLINE = { level: "offline", label: "App not running", advice: "Can't reach the downloader. Start it with ./run.sh.", bars: 0 };
const CHECKING = { level: "checking", label: "Checking…", advice: "Testing the internet and YouTube…", bars: 0 };

const fmtMs = (p: any) => (p?.ok ? `${p.latency} ms` : p ? (p.error || "No response") : "—");

export function NetPill({ net }: { net: any }) {
  const n = net || CHECKING;
  return (
    <button className={`net-pill lvl-${n.level}`} id="net-pill" type="button" title="Connection"
      onClick={() => document.getElementById("net-card")?.scrollIntoView({ behavior: "smooth", block: "center" })}>
      <span className="dot" /><span id="net-pill-text">{n.label}</span>
    </button>
  );
}

export function NetPanel({ net }: { net: any }) {
  const { toast } = useApp();
  const n = net || { ...CHECKING, label: "Checking connection…" };
  const checkedRef = useRef<number | null>(null);
  checkedRef.current = n.checked ?? null;
  const [testing, setTesting] = useState(false);
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 1000); return () => clearInterval(t); }, []);

  const y = n.youtube;
  const age = n.checked ? Math.max(0, Math.round(Date.now() / 1000 - n.checked)) : null;
  const checkedText = age == null ? "" : `Checked ${age < 5 ? "just now" : age < 60 ? `${age}s ago` : `${Math.round(age / 60)} min ago`}`;

  async function testNow() {
    setTesting(true);
    const before = checkedRef.current;
    try {
      await api("/api/net/check", { method: "POST" });
      for (let i = 0; i < 60 && checkedRef.current === before; i++) await new Promise((r) => setTimeout(r, 500));
    } catch (err: any) { toast(err.message); }
    finally { setTesting(false); }
  }

  return (
    <section className={`card net lvl-${n.level}`} id="net-card" aria-live="polite">
      <div className="net-main">
        <div className="signal" id="net-bars" aria-hidden="true">
          {[0, 1, 2, 3].map((i) => <i key={i} className={i < (n.bars || 0) ? "on" : ""} />)}
        </div>
        <div className="net-text">
          <div className="net-label"><span id="net-label">{n.label}</span></div>
          <div className="net-advice" id="net-advice">{n.advice || ""}</div>
        </div>
        <button className="small" id="btn-net-check" type="button" disabled={testing} onClick={testNow}>{testing ? "Testing…" : "Test now"}</button>
      </div>
      <div className="net-stats">
        <div><span>YouTube response</span><b id="net-yt" className={n.youtube && !n.youtube.ok ? "bad" : ""}>{fmtMs(n.youtube)}</b></div>
        <div><span>Internet response</span><b id="net-inet" className={n.internet && !n.internet.ok ? "bad" : ""}>{fmtMs(n.internet)}</b></div>
        <div><span>Stability</span><b id="net-stab">{!y?.ok ? "—" : y.loss ? `${y.loss}% lost` : y.jitter < 150 ? "Stable" : `Unsteady (±${y.jitter} ms)`}</b></div>
        <div><span>Download speed</span><b id="net-speed">{!net ? "—" : n.speed ? fmtSpeed(n.speed) : n.peak_speed ? `Idle (peak ${fmtSpeed(n.peak_speed)})` : "Idle"}</b></div>
      </div>
      <div className="net-chart chart">
        <LineChart id="net-spark" width={400} height={44} ariaLabel="YouTube response time history" values={n.history || []}
          opts={{
            slots: 40, min: 500, gaps: true, fmt: (v) => `${Math.round(v)} ms`,
            label: (i, len) => { const ago = (len - 1 - i) * 10; return ago ? `${ago < 60 ? `${ago}s` : `${Math.round(ago / 60)} min`} ago` : "latest check"; },
          }} />
        <div className="net-foot"><span>YouTube response time, last few minutes</span><span id="net-checked">{checkedText}</span></div>
      </div>
    </section>
  );
}
