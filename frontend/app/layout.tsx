import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "CFP Conference Scout",
  description: "Standalone conference/CFP discovery tool",
  robots: { index: false, follow: false },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="antialiased bg-background text-foreground">
        {children}
      </body>
    </html>
  );
}
