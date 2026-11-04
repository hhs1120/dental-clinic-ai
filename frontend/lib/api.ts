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
