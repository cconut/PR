import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "EvoAgent Console",
  description: "PR risk governance and Agent runtime operations console",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
