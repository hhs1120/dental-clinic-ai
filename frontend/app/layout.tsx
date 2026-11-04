import type { Metadata } from "next";
import { Inter, Noto_Sans_KR } from "next/font/google";
import "./globals.css";

const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });

// 한글 글리프는 subsets에 없어 미리 불러오지 않는다
const notoKr = Noto_Sans_KR({
  variable: "--font-noto-kr",
  weight: ["400", "500", "700"],
  preload: false,
});

export const metadata: Metadata = {
  title: "치과 AX",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ko" className={`${inter.variable} ${notoKr.variable}`}>
      <body className="bg-background font-sans text-foreground antialiased">
        {children}
      </body>
    </html>
  );
}
