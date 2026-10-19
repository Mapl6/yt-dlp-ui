// Words for statuses, shared by the library and the dashboard.

export const JOB_LABEL: Record<string, string> = {
  saved: "Saved", queued: "Queued", downloading: "Downloading", pausing: "Pausing…", paused: "Paused", complete: "All downloaded",
};
export const ITEM_LABEL: Record<string, string> = {
  new: "Not downloaded", queued: "Queued", downloading: "Downloading", processing: "Processing",
  done: "Downloaded", skipped: "Downloaded", failed: "Failed",
};
export const SYNC_LABEL: Record<number | string, string> = { 6: "every 6 hours", 12: "every 12 hours", 24: "daily", 168: "weekly" };
export const REVEAL_LABEL = typeof navigator !== "undefined" && /Mac/i.test(navigator.platform) ? "Show in Finder" : "Show in folder";
