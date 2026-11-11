// 끝 정리 — data/ 복사본(수백 MB)과 테스트 서버의 빌드 폴더(.next-e2e)를 지우고, 테스트 서버가 바꿔 둔
// next-env.d.ts를 켜 둔 make dev의 .next 쪽으로 되돌린다(next typegen). 서버가 아직 살아 있어 .next-e2e 일부가
// 다시 생길 수 있다 — 다음 시작 때 비운다(playwright.config.ts)
import { execSync } from "node:child_process";
import { rmSync } from "node:fs";
import { resolve } from "node:path";

export default function globalTeardown() {
  const dir = process.env.AX_E2E_DIR;
  if (dir) rmSync(dir, { recursive: true, force: true });
  const frontend = resolve(__dirname, "..");
  rmSync(resolve(frontend, ".next-e2e"), { recursive: true, force: true });
  execSync("npx next typegen", { cwd: frontend, stdio: "ignore" });
}
