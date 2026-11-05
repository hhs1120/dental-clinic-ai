// 앱 API 타입과 호출 — 응답 모양의 원본은 backend/api.py (앱 뼈대 설계안 '백엔드')
import { useEffect, useState } from "react";

export type SourceType = "document" | "call" | "message_thread" | "chart_note";

export interface SearchCard {
  id: string;
  source_type: SourceType;
  title: string | null;
  body: string;
  event_date: string | null;
  chart_no: string | null;
  patient_name: string | null;
  caller_name: string | null;
  direction: "수신" | "발신" | null;
  doc_type: string | null;
  version: string | null;
  is_outdated: boolean | null;
}

export interface SearchResponse {
  manual_results: SearchCard[];
  case_results: SearchCard[];
}

export interface Clinic {
  name: string;
  today: string;
}

export class ApiError extends Error {}

const NO_SERVER = "서버에 연결하지 못했습니다";

/** API 오류는 detail 문구로, 프록시 실패처럼 JSON이 아니면 연결 실패 문구로 던진다 */
export async function getJson<T>(
  path: string,
  params?: URLSearchParams,
  signal?: AbortSignal,
): Promise<T> {
  const query = params?.toString();
  let res: Response;
  try {
    res = await fetch(`/api${path}${query ? `?${query}` : ""}`, { signal });
  } catch (e) {
    if (signal?.aborted) throw e;
    throw new ApiError(NO_SERVER);
  }
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    throw new ApiError(NO_SERVER);
  }
  if (!res.ok) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") throw new ApiError(detail);
    if (Array.isArray(detail))
      throw new ApiError(detail.map((d: { msg?: string }) => d.msg).join(", "));
    throw new ApiError(NO_SERVER);
  }
  return body as T;
}

/** POST JSON — 오류 처리는 getJson과 같다(detail 문구, 아니면 연결 실패) */
export async function postJson<T>(path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  } catch {
    throw new ApiError(NO_SERVER);
  }
  let data: unknown;
  try {
    data = await res.json();
  } catch {
    throw new ApiError(NO_SERVER);
  }
  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail;
    if (typeof detail === "string") throw new ApiError(detail);
    if (Array.isArray(detail))
      throw new ApiError(detail.map((d: { msg?: string }) => d.msg).join(", "));
    throw new ApiError(NO_SERVER);
  }
  return data as T;
}

export const errorText = (e: unknown) =>
  e instanceof ApiError ? e.message : NO_SERVER;

/** 화면 진입 때 조회 — key가 바뀌거나 reload()하면 다시 가져온다. 불러오는 중 data·error 모두 null */
export function useFetch<T>(
  key: string | null,
  load: (signal: AbortSignal) => Promise<T>,
): {
  data: T | null;
  error: string | null;
  loading: boolean; // 다시 불러오는 중(이전 값을 보이는 동안 포함)
  reload: () => void;
} {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<{
    key: string;
    data?: T;
    error?: string;
  } | null>(null);
  useEffect(() => {
    if (key === null) return;
    const ac = new AbortController();
    load(ac.signal).then(
      (data) => setState({ key: `${key}#${tick}`, data }),
      (e) =>
        !ac.signal.aborted &&
        setState({ key: `${key}#${tick}`, error: errorText(e) }),
    );
    return () => ac.abort();
    // load는 key로 정해진다고 본다 — 렌더마다 새로 만든 함수로 다시 부르지 않게
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick]);
  // 새로 불러오는 동안에는 이전 값을 보인다(체크 뒤 깜빡이지 않게), 다른 key면 비운다
  const same = state && key !== null && state.key.startsWith(`${key}#`);
  return {
    data: same ? (state.data ?? null) : null,
    error: same ? (state.error ?? null) : null,
    loading: key !== null && state?.key !== `${key}#${tick}`,
    reload: () => setTick((t) => t + 1),
  };
}

let clinicRequest: Promise<Clinic> | null = null;

/** 병원 이름·기준일 — 앱에서 한 번만 가져온다. 불러오는 중 null, 실패 "error" */
export function useClinic(): Clinic | null | "error" {
  const [clinic, setClinic] = useState<Clinic | null | "error">(null);
  useEffect(() => {
    let live = true;
    clinicRequest ??= getJson<Clinic>("/clinic");
    clinicRequest.then(
      (c) => live && setClinic(c),
      () => {
        clinicRequest = null; // 다음 화면에서 다시 시도한다
        if (live) setClinic("error");
      },
    );
    return () => {
      live = false;
    };
  }, []);
  return clinic;
}
