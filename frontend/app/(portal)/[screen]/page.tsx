import { notFound } from "next/navigation";

import { ComingSoon } from "@/components/coming-soon";
import { COMING_SOON } from "@/lib/nav";

export function generateStaticParams() {
  return Object.keys(COMING_SOON).map((screen) => ({ screen }));
}

export default async function Page({ params }: PageProps<"/[screen]">) {
  const { screen } = await params;
  const title = COMING_SOON[screen];
  if (!title) notFound(); // cacheComponents에서는 dynamicParams를 쓰지 못한다
  return <ComingSoon title={title} />;
}
