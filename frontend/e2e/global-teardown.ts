// 복사본 지우기 — 실행마다 data/ 복사본(수백 MB)이 임시 폴더에 쌓이지 않게
import { rmSync } from "node:fs";

export default function globalTeardown() {
  const dir = process.env.AX_E2E_DIR;
  if (dir) rmSync(dir, { recursive: true, force: true });
}
