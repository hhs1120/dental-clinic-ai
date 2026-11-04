"use client";

import Link from "next/link";

import { Card } from "@/components/ui/card";
import { useClinic } from "@/lib/api";

// 시작 화면 — 메뉴 없음, 카드 4개로 갈라진다(기능 정의 §4 앱 구성, 와이어프레임 R.home)
const TILES = [
  {
    href: "/today",
    title: "사내 포털",
    sub: "오늘 · 통화 · 준비표 · 청구 · 재무",
    who: "전 직원",
  },
  {
    href: "/ask",
    title: "물어보기",
    sub: "매뉴얼·공지·통화·문자·차트 검색과 질문",
    who: "전 직원",
  },
  {
    href: "/eval",
    title: "AI 평가",
    sub: "엇갈림 판정 · 블라인드 비교 · 정확도 표",
    who: "원장 · 판정 담당 · 일상 업무와 분리",
  },
  {
    href: "/emr",
    title: "전환 전 EMR",
    sub: "데스크 · 예약 · 전자차트 · 준비표와 비교",
    who: "읽기 전용 · 비교용",
  },
];

export default function Home() {
  const clinic = useClinic();
  const name = clinic === "error" ? "—" : (clinic?.name ?? "");
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col items-center px-4 py-16 text-center">
      <span className="flex size-12 items-center justify-center rounded-xl bg-primary text-lg font-bold text-primary-foreground">
        AX
      </span>
      <h1 className="mt-4 min-h-9 text-3xl font-bold">{name}</h1>
      <p className="text-muted-foreground">Dental · 사내 시스템</p>
      <div className="mt-8 grid w-full grid-cols-1 gap-4 text-left sm:grid-cols-2">
        {TILES.map((t) => (
          <Link key={t.href} href={t.href} className="group rounded-xl">
            <Card className="h-full gap-2 p-6 shadow-card group-hover:ring-primary">
              <div className="text-lg font-bold">{t.title}</div>
              <div className="text-sm text-muted-foreground">{t.sub}</div>
              <div className="mt-1 text-xs text-muted-foreground">{t.who}</div>
            </Card>
          </Link>
        ))}
      </div>
    </main>
  );
}
