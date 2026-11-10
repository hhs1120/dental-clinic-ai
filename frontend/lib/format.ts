// 화면 공통 표기 — 금액은 모든 화면이 이 함수 하나로 쓴다(전환 전 EMR 창 안은 emr-ui.tsx commas)

/** 12340 → 12,340원 */
export const won = (n: number) => `${n.toLocaleString("ko-KR")}원`;
