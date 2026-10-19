// What the server sends. Only the fields the page reads are listed; the server may send more.

export interface Item {
  id: string;
  index: number;
  title: string;
  status: string;
  duration?: number | null;
  removed?: boolean;
  finished?: number | null;
  progress?: number;
  downloaded?: number;
  total?: number;
  speed?: number | null;
  eta?: number | null;
  step?: string | null;
  filepath?: string | null;
  section?: { start: number; end: number | null } | null;
  health?: string | null;
  held?: boolean;
  error?: string | null;
  [k: string]: any;
}

export interface Counts {
  new: number; queued: number; downloading: number; processing: number; done: number; failed: number; removed: number;
}

export interface Job {
  id: string;
  title: string;
  kind: string;
  status: string;
  total: number;
  percent: number;
  counts: Counts;
  rev: number;
  paused?: boolean;
  thumbnail?: string | null;
  heights?: number[];
  options?: Record<string, any>;
  outdir: string;
  speed?: number;
  check?: { running: boolean; done: number; total: number; finished?: number; ok: number; found: number; damaged: number; missing: number } | null;
  sync?: { every: number; auto: boolean; error?: string | null } | null;
  [k: string]: any;
}

export interface Overview {
  jobs: Job[];
  settings: Record<string, any>;
  active: number;
  speed: number;
  waiting: string | null;
  import: { running: boolean; done: number; total: number; added: number; failed: { url: string; error: string }[] } | null;
  cookie_file: { imported: number; count: number; youtube: number; signed_in: boolean } | null;
  last_event?: number;
  net?: Record<string, any>;
  [k: string]: any;
}

export interface Activity {
  active: any[];
  queue: any[];
  queued: number;
  recent: any[];
  jobs: { id: string; title: string }[];
  stats: Record<string, any>;
  [k: string]: any;
}
