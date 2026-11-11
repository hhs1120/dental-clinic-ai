// 화면 E2E(기능 정의 §4 ② 완료 기준) — data/를 임시 폴더로 복사해 그 복사본으로 API(8100)·화면(3100)을 띄운다.
// 진짜 판정 DB를 더럽히지 않게. 실데이터가 필요해 CI에는 넣지 않는다(make e2e, 로컬). 브라우저는 설치된 Chrome
import { cpSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { defineConfig } from "@playwright/test";

const root = resolve(__dirname, "..");
// 설정은 작업자마다 다시 읽히므로 한 번만 복사한다
process.env.AX_E2E_DIR ??= (() => {
  // 테스트 서버 빌드 폴더를 서버를 띄우기 전에 비운다 — 끝 정리 뒤 꺼지던 서버가 일부(lock 포함)를 다시 만들어 두면
  // 다음 실행이 깨진 캐시로 처음부터 다시 빌드한다(수 분)
  rmSync(join(__dirname, ".next-e2e"), { recursive: true, force: true });
  const dir = mkdtempSync(join(tmpdir(), "ax-e2e-"));
  // app.sqlite는 WAL(앱 설계 §6)이라 아직 합쳐지지 않은 쓰기가 -wal 파일에 있을 수 있다 — 함께 복사
  for (const f of ["app.sqlite", "app.sqlite-wal", "source.sqlite"])
    if (existsSync(join(root, "data", f)))
      cpSync(join(root, "data", f), join(dir, f));
  return dir;
})();

export default defineConfig({
  testDir: "e2e",
  globalTeardown: "./e2e/global-teardown.ts",
  // 파일 단위 병렬(파일 안은 차례로) — 4개 넘게 늘려도 거의 빨라지지 않는다. DB 복사본은 함께 쓴다
  workers: 4,
  timeout: 120_000,
  use: { baseURL: "http://localhost:3100", channel: "chrome" },
  webServer: [
    {
      command: `uv run uvicorn backend.api:app --port 8100`,
      cwd: root,
      env: { AX_DATA_DIR: process.env.AX_E2E_DIR },
      url: "http://localhost:8100/clinic",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npx next dev -p 3100",
      env: { AX_API_URL: "http://localhost:8100", NEXT_DIST_DIR: ".next-e2e" },
      url: "http://localhost:3100/eval",
      reuseExistingServer: false,
      timeout: 180_000,
    },
  ],
});
