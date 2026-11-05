import { notFound } from "next/navigation";

import { ComingSoon } from "@/components/coming-soon";
import { COMING_SOON } from "@/lib/nav";

// 준비 중 임시 화면 — 화면 이름(params)을 기다리느라 바로 이동하지 않아도 된다(Next.js instant 경고)
export const instant = false;

export function generateStaticParams() {
  return Object.keys(COMING_SOON).map((screen) => ({ screen }));
}

export default async function Page({ params }: PageProps<"/[screen]">) {
  const { screen } = await params;
  // cacheComponents에서는 dynamicParams를 쓰지 못한다. hasOwn — /constructor 같은 상속 키를 막는다
  if (!Object.hasOwn(COMING_SOON, screen)) notFound();
  return <ComingSoon title={COMING_SOON[screen]} />;
}
