// The page shell. FastAPI adds the session token (<meta name="api-token">) to <head> when it serves the built
// page; React doesn't render that tag, or it would add its own empty copy when it hydrates.
import type { Metadata, Viewport } from "next";
import "../static/style.css";

const ICON = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='9' fill='%23d93a32'/%3E%3Cpath d='M16 8v12m0 0-5-5m5 5 5-5M10 24h12' fill='none' stroke='white' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E";

export const metadata: Metadata = { title: "YT Downloader", icons: { icon: ICON } };
export const viewport: Viewport = { width: "device-width", initialScale: 1, colorScheme: "light dark" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
