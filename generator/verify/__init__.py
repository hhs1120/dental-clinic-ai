"""make verify — 생성 데이터 검사. 검사 목록은 docs/치과 AX - 가상 데이터 생성 설계.md §5.3.
필수 검사 실패 시 exit 1, 경고는 위반만 보여 주고, 리포트(report_*)는 허용치 없이 수치만 출력한다(튜닝 때 눈으로 봄).
검사는 영역별 파일(common·schedule·records·chart·documents·rag_qa·calls·messages·fin)에, 목록(REQUIRED·WARNINGS·REPORTS)·
실행·결정성 해시는 여기에 둔다. 바깥에서는 verify.check_*처럼 이 패키지 이름으로 읽는다.

구현: 정형(emr_* — 예약·치료계획·이탈·진료·청구·수납·주의사항)·차트 사실·서술(5b·9b)·ax_calls·통화 슬롯(6단계)·전사(7단계)·
문자(8단계)·재무(8b 수입·8c 지출)·RAG 문서(9단계)·
RAG 질문(truth_rag_qa 있을 때) 필수 검사 + 분포·치아·통화 분포·전사·RAG 질문 경고 + 리포트.
§5.3 중 아직 없는 것(해당 단계 구현 때 추가). 청크 검사는 구현 전까지 [SKIP]으로 출력:
  필수 — 다형 참조(ax_llm_calls.ref_*·ax_chunks.source_*), 청크 prefix·정답 청크가 ax_chunks에 존재(app.sqlite를 붙였을 때 — --app),
         누출(분류기 입력 덤프 — 풀 순번·call_uid 셔플은 tests/generator/test_calls.py, 행 순서는 check_call_row_order)
결정성 2회 비교는 make verify가 아니라 tests/generator/test_generator.py — canonical_hash로, 두 번은 서로 다른
PYTHONHASHSEED(0과 1)로 (같은 값이면 set 순회 순서에 기댄 버그가 가려짐)
검사 함수는 (conn, ctx) → 위반 행 목록(dict). 빈 목록이면 통과. 리포트는 (conn, ctx) → 수치 dict.
conn은 source.sqlite(생성기 출력)이고 truth.sqlite가 `truth` 스키마로, --app을 주면 app.sqlite가 `app` 스키마로 ATTACH되어 있다.
"""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from generator.master import ADJUST_REASONS, CHAIRS, DOCTOR_CONCURRENCY
from generator.paths import output_paths
from generator.verify.calls import (
    AX_CALLS_COLUMNS,
    CALL_MIX_TOLERANCE,
    CALL_UID,
    CLOSE_TIME,
    OPEN_TIME,
    TRANSCRIPT_LABEL_WORD_MAX,
    check_already_paid_calls,
    check_ax_calls_columns,
    check_blind_pool,
    check_booking_call_event,
    check_call_counterpart,
    check_call_day,
    check_call_duration,
    check_call_facts_as_of,
    check_call_label_values,
    check_call_labels_match,
    check_call_noise_mix,
    check_call_purpose_mix,
    check_call_row_order,
    check_call_slot_count,
    check_call_transcripts,
    check_call_uid_format,
    check_churn_calls,
    check_eval_pool,
    check_eval_pool_strata,
    check_followup_topic,
    check_label_tokens,
    check_transcript_facts,
    check_transcript_label_words,
    check_transcript_speakers,
    report_call_mix,
)
from generator.verify.chart import (
    CHART_DECOY_RANGE,
    CHART_NOTE_COLUMNS,
    _chart_lines,
    check_alert_ends,
    check_alert_recorded_at,
    check_alert_source,
    check_alert_target,
    check_alerts_per_patient,
    check_chart_decoy_ratio,
    check_chart_dx,
    check_chart_evidence_words,
    check_chart_narrative_facts,
    check_chart_note_columns,
    check_chart_note_per_treatment,
    check_chart_plan,
    check_chart_requirement,
    check_chart_special,
    check_chart_text,
    check_claim_errors,
    check_omitted_extra,
    check_requirement_facts,
    report_adjust_judgment,
    report_alert_holder_share,
    report_claim_errors,
)
from generator.verify.common import (
    CHILD_MAX_AGE,
    EXAMPLES,
    PROCEDURE,
    REASON_BY_TEXT,
    ROOT,
    Ctx,
    check_foreign_keys,
    check_no_records_after_today,
    check_status_from_today,
    check_timestamps_not_future,
    load_ctx,
)
from generator.verify.documents import (
    DOC_PATH,
    check_claim_sections,
    check_doc_external_clean,
    check_doc_key_leak,
    check_doc_meta_match,
    check_doc_patient_names,
    check_doc_planted_facts,
    check_doc_similar_clean,
    check_document_counts,
    check_forbidden_topics,
    check_notice_versions,
)
from generator.verify.fin import (
    check_bank_inflows,
    check_card_deposits,
    check_card_sales,
    check_expense_matches,
    check_lab_statements,
    check_recon_mismatches,
)
from generator.verify.messages import (
    APPOINTMENT_TRIGGERS,
    check_churn_no_reply,
    check_confirmed_has_reminder,
    check_fake_phone,
    check_message_once,
    check_message_triggers,
    check_paid_replies,
    check_recall_once_a_month,
    check_reply_after_out,
    report_message_counts,
)
from generator.verify.rag_qa import (
    QA_CHART_ANSWERS,
    QA_COPY_RUN,
    QA_REFUSE_REASONS,
    QA_SOURCE_ID,
    QA_SOURCES,
    QA_TOOL_ARGS,
    QA_TYPE_COUNTS,
    check_qa_answer_ids,
    check_qa_answer_sql,
    check_qa_copied_wording,
    check_qa_counts,
    check_qa_distractor_clean,
    check_qa_fields,
    check_qa_matches_json,
    check_qa_patient_names,
    check_qa_refusal,
    check_qa_refusal_absent,
    check_qa_same_fact_docs,
    check_qa_split_isolation,
    has_qa,
    qa_stems,
)
from generator.verify.records import (
    PREP_SHEET_DAYS,
    _submitted_months,
    check_adjust_reason,
    check_approval_no,
    check_balance_non_negative,
    check_child_procedures,
    check_completed_one_treatment,
    check_extracted_tooth,
    check_insured_claim_one_line,
    check_last_month_claims_pending,
    check_ledger_error_unsettled,
    check_ledger_errors,
    check_parallel_external,
    check_payment_per_visit,
    check_performed_at,
    check_plan_item_appointments,
    check_plan_item_status,
    check_plan_item_values,
    check_plan_origin,
    check_registration_age,
    check_reviewed_at,
    check_scaling_limit,
    check_service_date,
    check_settled_claims,
    check_submitted_months_no_result,
    check_tooth_level,
    check_tooth_treatment_age,
    check_treatment_amounts,
    check_treatment_on_completed,
    check_uninsured_not_claimed,
    check_visit_payment_completed,
    report_balance_holder_share,
    report_open_plans,
)
from generator.verify.schedule import (
    VISIT_MIX_TOLERANCE,
    check_cancel_time,
    check_cancellation_kind,
    check_cancelled_at_status,
    check_chair_overlap,
    check_churn_after,
    check_churn_date,
    check_confirmed_not_future,
    check_confirmed_reminded,
    check_consult_no_chair,
    check_created_before_scheduled,
    check_doctor_concurrency,
    check_hygienist_concurrency,
    check_patient_same_day,
    check_pre_window_visit,
    check_reschedule_pair,
    check_visit_mix,
    report_churn,
    report_daily_fill,
)

__all__ = [
    "ADJUST_REASONS",
    "APPOINTMENT_TRIGGERS",
    "AX_CALLS_COLUMNS",
    "CALL_MIX_TOLERANCE",
    "CALL_UID",
    "CHAIRS",
    "CHART_DECOY_RANGE",
    "CHART_NOTE_COLUMNS",
    "CHILD_MAX_AGE",
    "CLOSE_TIME",
    "Ctx",
    "DOCTOR_CONCURRENCY",
    "DOC_PATH",
    "EXAMPLES",
    "OPEN_TIME",
    "PREP_SHEET_DAYS",
    "PROCEDURE",
    "QA_CHART_ANSWERS",
    "QA_COPY_RUN",
    "QA_REFUSE_REASONS",
    "QA_SOURCES",
    "QA_SOURCE_ID",
    "QA_TOOL_ARGS",
    "QA_TYPE_COUNTS",
    "REASON_BY_TEXT",
    "REPORTS",
    "REQUIRED",
    "ROOT",
    "TRANSCRIPT_LABEL_WORD_MAX",
    "VISIT_MIX_TOLERANCE",
    "WARNINGS",
    "_chart_lines",
    "_submitted_months",
    "canonical_hash",
    "check_adjust_reason",
    "check_alert_ends",
    "check_alert_recorded_at",
    "check_alert_source",
    "check_alert_target",
    "check_alerts_per_patient",
    "check_already_paid_calls",
    "check_approval_no",
    "check_ax_calls_columns",
    "check_balance_non_negative",
    "check_bank_inflows",
    "check_blind_pool",
    "check_booking_call_event",
    "check_call_counterpart",
    "check_call_day",
    "check_call_duration",
    "check_call_facts_as_of",
    "check_call_label_values",
    "check_call_labels_match",
    "check_call_noise_mix",
    "check_call_purpose_mix",
    "check_call_row_order",
    "check_call_slot_count",
    "check_call_transcripts",
    "check_call_uid_format",
    "check_cancel_time",
    "check_cancellation_kind",
    "check_cancelled_at_status",
    "check_card_deposits",
    "check_card_sales",
    "check_chair_overlap",
    "check_chart_decoy_ratio",
    "check_chart_dx",
    "check_chart_evidence_words",
    "check_chart_narrative_facts",
    "check_chart_note_columns",
    "check_chart_note_per_treatment",
    "check_chart_plan",
    "check_chart_requirement",
    "check_chart_special",
    "check_chart_text",
    "check_child_procedures",
    "check_churn_after",
    "check_churn_calls",
    "check_churn_date",
    "check_churn_no_reply",
    "check_claim_errors",
    "check_claim_sections",
    "check_completed_one_treatment",
    "check_confirmed_has_reminder",
    "check_confirmed_not_future",
    "check_confirmed_reminded",
    "check_consult_no_chair",
    "check_created_before_scheduled",
    "check_doc_external_clean",
    "check_doc_key_leak",
    "check_doc_meta_match",
    "check_doc_patient_names",
    "check_doc_planted_facts",
    "check_doc_similar_clean",
    "check_doctor_concurrency",
    "check_document_counts",
    "check_eval_pool",
    "check_eval_pool_strata",
    "check_expense_matches",
    "check_extracted_tooth",
    "check_fake_phone",
    "check_followup_topic",
    "check_forbidden_topics",
    "check_foreign_keys",
    "check_hygienist_concurrency",
    "check_insured_claim_one_line",
    "check_lab_statements",
    "check_label_tokens",
    "check_last_month_claims_pending",
    "check_ledger_error_unsettled",
    "check_ledger_errors",
    "check_message_once",
    "check_message_triggers",
    "check_no_records_after_today",
    "check_notice_versions",
    "check_omitted_extra",
    "check_paid_replies",
    "check_parallel_external",
    "check_patient_same_day",
    "check_payment_per_visit",
    "check_performed_at",
    "check_plan_item_appointments",
    "check_plan_item_status",
    "check_plan_item_values",
    "check_plan_origin",
    "check_pre_window_visit",
    "check_qa_answer_ids",
    "check_qa_answer_sql",
    "check_qa_copied_wording",
    "check_qa_counts",
    "check_qa_distractor_clean",
    "check_qa_fields",
    "check_qa_matches_json",
    "check_qa_patient_names",
    "check_qa_refusal",
    "check_qa_refusal_absent",
    "check_qa_same_fact_docs",
    "check_qa_split_isolation",
    "check_recall_once_a_month",
    "check_recon_mismatches",
    "check_registration_age",
    "check_reply_after_out",
    "check_requirement_facts",
    "check_reschedule_pair",
    "check_reviewed_at",
    "check_scaling_limit",
    "check_service_date",
    "check_settled_claims",
    "check_status_from_today",
    "check_submitted_months_no_result",
    "check_timestamps_not_future",
    "check_tooth_level",
    "check_tooth_treatment_age",
    "check_transcript_facts",
    "check_transcript_label_words",
    "check_transcript_speakers",
    "check_treatment_amounts",
    "check_treatment_on_completed",
    "check_uninsured_not_claimed",
    "check_visit_mix",
    "check_visit_payment_completed",
    "has_qa",
    "load_ctx",
    "main",
    "qa_stems",
    "report_adjust_judgment",
    "report_alert_holder_share",
    "report_balance_holder_share",
    "report_call_mix",
    "report_churn",
    "report_claim_errors",
    "report_daily_fill",
    "report_message_counts",
    "report_open_plans",
    "run",
]


REQUIRED = [
    check_foreign_keys,
    check_tooth_treatment_age,
    check_registration_age,
    check_chart_plan,
    check_chart_evidence_words,
    check_status_from_today,
    check_no_records_after_today,
    check_timestamps_not_future,
    check_last_month_claims_pending,
    check_submitted_months_no_result,
    check_settled_claims,
    check_parallel_external,
    check_reviewed_at,
    check_patient_same_day,
    check_chair_overlap,
    check_hygienist_concurrency,
    check_doctor_concurrency,
    check_consult_no_chair,
    check_confirmed_not_future,
    check_confirmed_reminded,
    check_created_before_scheduled,
    check_cancel_time,
    check_cancelled_at_status,
    check_cancellation_kind,
    check_reschedule_pair,
    check_pre_window_visit,
    check_churn_date,
    check_churn_after,
    check_plan_origin,
    check_plan_item_appointments,
    check_plan_item_status,
    check_plan_item_values,
    check_treatment_on_completed,
    check_completed_one_treatment,
    check_performed_at,
    check_service_date,
    check_uninsured_not_claimed,
    check_scaling_limit,
    check_extracted_tooth,
    check_treatment_amounts,
    check_insured_claim_one_line,
    check_adjust_reason,
    check_balance_non_negative,
    check_visit_payment_completed,
    check_payment_per_visit,
    check_approval_no,
    check_ledger_errors,
    check_ledger_error_unsettled,
    check_alert_target,
    check_alert_source,
    check_alert_recorded_at,
    check_alert_ends,
    check_alerts_per_patient,
    check_chart_note_per_treatment,
    check_chart_dx,
    check_claim_errors,
    check_omitted_extra,
    check_chart_requirement,
    check_requirement_facts,
    check_chart_special,
    check_chart_text,
    check_chart_narrative_facts,
    check_chart_note_columns,
    check_ax_calls_columns,
    check_call_uid_format,
    check_call_labels_match,
    check_call_label_values,
    check_call_slot_count,
    check_call_day,
    check_call_duration,
    check_call_transcripts,
    check_call_row_order,
    check_booking_call_event,
    check_call_counterpart,
    check_call_facts_as_of,
    check_churn_calls,
    check_followup_topic,
    check_already_paid_calls,
    check_eval_pool,
    check_blind_pool,
    check_eval_pool_strata,
    check_message_triggers,
    check_message_once,
    check_recall_once_a_month,
    check_churn_no_reply,
    check_paid_replies,
    check_confirmed_has_reminder,
    check_reply_after_out,
    check_fake_phone,
    check_card_sales,
    check_card_deposits,
    check_bank_inflows,
    check_recon_mismatches,
    check_expense_matches,
    check_lab_statements,
    check_document_counts,
    check_doc_meta_match,
    check_doc_planted_facts,
    check_doc_similar_clean,
    check_doc_external_clean,
    check_doc_key_leak,
    check_notice_versions,
    check_claim_sections,
    check_doc_patient_names,
    check_forbidden_topics,
    check_qa_matches_json,
    check_qa_fields,
    check_qa_counts,
    check_qa_answer_ids,
    check_qa_split_isolation,
    check_qa_same_fact_docs,
    check_qa_answer_sql,
    check_qa_distractor_clean,
    check_qa_patient_names,
    check_qa_refusal,
]


WARNINGS = [
    check_visit_mix,
    check_tooth_level,
    check_child_procedures,
    check_chart_decoy_ratio,
    check_call_purpose_mix,
    check_call_noise_mix,
    check_transcript_speakers,
    check_transcript_facts,
    check_transcript_label_words,
    check_label_tokens,
    check_qa_refusal_absent,
    check_qa_copied_wording,
]


REPORTS = [
    report_daily_fill,
    report_balance_holder_share,
    report_alert_holder_share,
    report_open_plans,
    report_churn,
    report_adjust_judgment,
    report_claim_errors,
    report_call_mix,
    report_message_counts,
]


def canonical_hash(conn, schema: str = "main") -> str:
    """한 스키마(main = source.sqlite, truth = truth.sqlite) 전 테이블의 내용 해시 — 테이블·컬럼 이름순, 행은 정렬.
    같은 시드 2회 비교용. 결정성은 두 스키마 모두 본다(실행마다 바뀌는 칸 — 색인·벡터 — 은 app.sqlite에만 있다)"""
    h = hashlib.sha256()
    tables = [
        r[0]
        for r in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in tables:
        cols = sorted(r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})"))
        rows = sorted(
            repr(tuple(r))
            for r in conn.execute(
                f"SELECT {', '.join(f'[{c}]' for c in cols)} FROM {schema}.[{table}]"
            )
        )
        h.update(json.dumps([table, cols, rows], ensure_ascii=False).encode())
    return h.hexdigest()


def run(conn, ctx) -> bool:
    """모든 검사를 돌려 리포트를 출력. 필수 검사가 하나라도 실패하면 False."""
    ok = True
    qa = has_qa(conn, ctx)
    for level, checks in (("FAIL", REQUIRED), ("WARN", WARNINGS)):
        for check in checks:
            name = getattr(check, "__name__", "check")
            if name.startswith("check_qa_") and not qa:
                continue
            violations = check(conn, ctx)
            if not violations:
                print(f"[ OK ] {name}")
                continue
            ok = ok and level != "FAIL"
            print(f"[{level}] {name}: {len(violations)}건 — {(check.__doc__ or '').strip()}")
            for v in violations[:EXAMPLES]:
                print(f"         {v}")
    # 청크 검사는 앱 색인 코드와 함께 구현 — 그 전엔 건너뜀으로 표시
    # — 청크가 생겨도 검사 없이 통과한 것처럼 보이지 않게 항상 출력
    print("[SKIP] RAG 청크(prefix·정답 청크 존재) — 미구현(앱 색인 코드 후)")
    if not qa:
        print(
            "[SKIP] RAG 질문 — truth_rag_qa 비어 있음(ground_truth/rag_qa.json 작성 후 make data)"
        )
    for report in REPORTS:
        print(f"[INFO] {report.__name__}: {report(conn, ctx)}")
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, help="경로를 직접 줄 때(테스트용). 기본은 generator/paths.py"
    )
    parser.add_argument("--truth", type=Path)
    parser.add_argument(
        "--app", type=Path, help="app.sqlite — 주면 앱 쪽 검사(색인·LLM 로그)를 붙여서 본다"
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "generator.yaml")
    parser.add_argument(
        "--rag-qa",
        type=Path,
        help="이 DB와 짝인 rag_qa.json. 기본은 --db를 안 줄 때만 generator/paths.py",
    )
    args = parser.parse_args(argv)
    paths = output_paths()
    db, truth_db = args.db or paths["source"], args.truth or paths["truth"]
    for path in (db, truth_db):
        if not path.exists():
            print(f"DB 없음: {path} — make data 먼저")
            return 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (f"file:{truth_db}?mode=ro",))
    if args.app:
        conn.execute("ATTACH DATABASE ? AS app", (f"file:{args.app}?mode=ro",))
    try:
        ctx = load_ctx(
            args.config, conn, args.rag_qa or (paths["rag_qa"] if args.db is None else None)
        )
    except (KeyError, sqlite3.OperationalError) as e:
        print(f"truth_gen_meta 읽기 실패({e}) — make data 먼저")
        return 1
    ok = run(conn, ctx)
    print("verify 통과" if ok else "verify 실패 — 필수 검사 위반")
    return 0 if ok else 1
