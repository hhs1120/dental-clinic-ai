// 전환 전 EMR·예약표 API 타입과 호출 — 응답 모양의 원본은 backend/emr.py (앱 설계 §9)
import { getJson } from "@/lib/api";

export type ApptType = "treatment" | "hygiene" | "consult";
export type ApptStatus = "scheduled" | "completed" | "cancelled" | "noshow";

export interface Appt {
  id: number;
  time: string;
  duration: number;
  staff_id: number;
  staff_name: string | null;
  staff_role: string | null;
  type: ApptType;
  procedure: string;
  tooth: string | null;
  status: ApptStatus;
  unconfirmed: boolean;
  chart_no: string;
  name: string;
  new_patient: boolean;
  plan_step: { seq: number; total: number } | null;
  booked_via: "phone" | "desk";
  booked_at: string;
  call: { uid: string; started_at: string } | null;
}

export interface ScheduleDay {
  date: string;
  hours: {
    open: string;
    close: string;
    lunch: [string, string] | null;
    closed: boolean;
  };
  staff: { id: number; name: string; role: string }[];
  appointments: Appt[];
  range: { from: string; to: string };
  counts: {
    total: number;
    treatment: number;
    hygiene: number;
    consult: number;
    unconfirmed: number;
  };
}

export interface PatientSummary {
  balance: number;
  alerts: { code: string; label: string; category: string }[];
  last_visit: string | null;
  upcoming: {
    id: number;
    date: string;
    time: string;
    procedure: string;
    staff: string;
  }[];
  noshow_1y: number;
  cancelled_1y: number;
}

export interface PatientHit {
  chart_no: string;
  name: string;
  age: number;
  sex: string;
}

export interface PatientRecord {
  patient: PatientHit & {
    birth: string;
    phone: string;
    registered: string;
    private_insurance: boolean;
    family: { chart_no: string; name: string }[];
    guardian: { chart_no: string; name: string } | null;
  };
  summary: PatientSummary;
  alerts: {
    code: string;
    label: string;
    category: string;
    recorded: string;
    ends: string | null;
    source: string;
  }[];
  appointments: {
    id: number;
    at: string;
    type: ApptType;
    procedure: string;
    status: ApptStatus;
    staff: string;
    chair: number | null;
    confirmed: boolean;
    plan_item_id: number | null;
  }[];
  treatments: {
    at: string;
    procedure: string;
    tooth: string;
    fee: number;
    insured: number;
    patient_amount: number;
    dx: string | null;
    note: string | null;
  }[];
  payments: {
    at: string;
    kind: string;
    due: number;
    paid: number;
    method: string;
  }[];
  claims: {
    month: string;
    service: string;
    procedure: string;
    amount: number;
    status: string;
    paid: number | null;
    adjust: string | null;
  }[];
  plans: {
    id: number;
    plan_id: number;
    seq: number;
    created: string;
    procedure: string;
    tooth: string;
    est_fee: number;
    status: string;
  }[];
  messages: {
    at: string;
    direction: "out" | "in";
    trigger: string;
    body: string;
    related_appointment_id: number | null;
  }[];
}

export interface TourPatient {
  chart_no: string;
  name: string;
  time: string;
  appointment_id: number;
  has: { balance: boolean; alert: boolean; remaining: boolean };
  appointments_today: number;
}

export const TYPE_LABEL: Record<ApptType, string> = {
  treatment: "진료",
  hygiene: "위생",
  consult: "상담",
};
export const STATUS_LABEL: Record<ApptStatus, string> = {
  scheduled: "예정",
  completed: "완료",
  cancelled: "취소",
  noshow: "노쇼",
};

const params = (o: Record<string, string>) => new URLSearchParams(o);

export const fetchSchedule = (date?: string, signal?: AbortSignal) =>
  getJson<ScheduleDay>(
    "/emr/schedule",
    date ? params({ date }) : undefined,
    signal,
  );
export const searchPatients = (q: string, signal?: AbortSignal) =>
  getJson<PatientHit[]>("/emr/patients", params({ q }), signal);
export const fetchPatient = (chartNo: string, signal?: AbortSignal) =>
  getJson<PatientRecord>(
    `/emr/patients/${encodeURIComponent(chartNo)}`,
    undefined,
    signal,
  );
export const fetchTour = (signal?: AbortSignal) =>
  getJson<TourPatient>("/emr/tour", undefined, signal);

const WEEK = "일월화수목금토";

/** 2026-12-01 → 12-01 (화) */
export function mdw(date: string): string {
  const d = new Date(`${date}T00:00:00`);
  return `${date.slice(5)} (${WEEK[d.getDay()]})`;
}

export const won = (n: number) => `${n.toLocaleString("ko-KR")}원`;
