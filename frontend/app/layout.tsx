import type { Metadata } from "next";
import { Inter } from "next/font/google";
import localFont from "next/font/local";
import "./globals.css";

import { THEME_SCRIPT } from "@/lib/theme";

const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });

// 한글은 Pretendard 가변 글꼴(npm 패키지를 빌드에 넣는다 — 실행 중 외부 CDN 없음). 파일이 커서 미리 불러오지 않는다
const pretendard = localFont({
  src: "../node_modules/pretendard/dist/web/variable/woff2/PretendardVariable.woff2",
  variable: "--font-pretendard",
  weight: "45 920",
  preload: false,
});

export const metadata: Metadata = {
  title: "치과 AX",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // data-theme은 아래 스크립트가 그리기 전에 붙인다 — 서버 HTML과 달라 경고를 끈다
    <html
      lang="ko"
      className={`${inter.variable} ${pretendard.variable}`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="bg-background font-sans text-foreground tabular-nums antialiased">
        {children}
      </body>
    </html>
  );
}
