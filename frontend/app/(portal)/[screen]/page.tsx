import { notFound } from "next/navigation";

import { ComingSoon } from "@/components/coming-soon";
import { COMING_SOON } from "@/lib/nav";

export function generateStaticParams() {
  return Object.keys(COMING_SOON).map((screen) => ({ screen }));
}

export default async function Page({ params }: PageProps<"/[screen]">) {
  const { screen } = await params;
  // cacheComponents에서는 dynamicParams를 쓰지 못한다. hasOwn — /constructor 같은 상속 키를 막는다
  if (!Object.hasOwn(COMING_SOON, screen)) notFound();
  return <ComingSoon title={COMING_SOON[screen]} />;
}
