"use client";
// The small dialogs: a video's details, "Remove playlist?" and the keyboard shortcuts.
import { forwardRef, useImperativeHandle, useRef, useState } from "react";
import { Detail } from "./Detail";

export const SHORTCUTS: [string, string][] = [
  ["/", "Search your playlists"],
  ["u", "Paste a link (focus the link box)"],
  ["p", "Pause or resume all downloads"],
  ["1 – 4", "Dashboard tabs: downloading, up next, recently finished, history & stats"],
  ["s", "Settings"],
  ["?", "Show these shortcuts"],
  ["Esc", "Close a dialog or menu"],
];

export interface LogDialogApi { show: (d: any) => void }

export const LogDialog = forwardRef<LogDialogApi>(function LogDialog(_, ref) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [d, setD] = useState<any>(null);
  useImperativeHandle(ref, () => ({ show(detail) { setD(detail); dlg.current?.showModal(); } }));
  return (
    <dialog id="log-dialog" ref={dlg}>
      <form method="dialog">
        <h3 id="log-title">{d?.title}</h3>
        <p className={`err ${d?.error ? "" : "hidden"}`} id="log-error">{d?.error || ""}</p>
        <div id="log-body">{d && <Detail d={d} />}</div>
        <p className="muted small-note">Full log: <code>data/app.log</code> in the app folder.</p>
        <div className="actions"><button value="close" className="primary">Close</button></div>
      </form>
    </dialog>
  );
});

export interface RemoveDialogApi { ask: (title: string) => Promise<{ deleteFiles: boolean } | null> }

/** Ask before removing a playlist; resolves to { deleteFiles } or null if cancelled. */
export const RemoveDialog = forwardRef<RemoveDialogApi>(function RemoveDialog(_, ref) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState("");
  const [files, setFiles] = useState(false);
  const filesRef = useRef(false);
  filesRef.current = files;
  useImperativeHandle(ref, () => ({
    ask(t) {
      setTitle(t);
      setFiles(false);
      const el = dlg.current!;
      el.returnValue = "";
      el.showModal();
      return new Promise((resolve) => {
        el.addEventListener("close", () => resolve(el.returnValue === "remove" ? { deleteFiles: filesRef.current } : null), { once: true });
      });
    },
  }));
  return (
    <dialog id="remove-dialog" ref={dlg}>
      <form method="dialog" className="stack">
        <h3 id="remove-title">{`Remove “${title}”?`}</h3>
        <p className="muted">It is removed from your library. You can add it again any time.</p>
        <label className="inline"><input type="checkbox" id="remove-files" checked={files} onChange={(e) => setFiles(e.target.checked)} />{" "}
          <span>Also delete its downloaded files
            <small>Only this playlist’s videos (files with their video id in the name) are deleted. Other files in the folder are kept.</small></span>
        </label>
        <p className={`err ${files ? "" : "hidden"}`} id="remove-warn">The files are deleted permanently.</p>
        <div className="actions">
          <button value="cancel" className="ghost">Cancel</button>
          <button value="remove" className="primary danger" id="remove-ok">{files ? "Remove and delete files" : "Remove"}</button>
        </div>
      </form>
    </dialog>
  );
});

export const KeysDialog = forwardRef<HTMLDialogElement>(function KeysDialog(_, ref) {
  return (
    <dialog id="keys-dialog" ref={ref}>
      <form method="dialog" className="stack">
        <h3>Keyboard shortcuts</h3>
        <dl className="keys" id="shortcut-list">
          {SHORTCUTS.map(([k, d]) => <Key key={k} k={k} d={d} />)}
        </dl>
        <div className="actions"><button value="close" className="primary">Close</button></div>
      </form>
    </dialog>
  );
});

const Key = ({ k, d }: { k: string; d: string }) => <><dt><kbd>{k}</kbd></dt><dd>{d}</dd></>;
