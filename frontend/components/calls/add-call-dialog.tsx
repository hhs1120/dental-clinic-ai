"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { errorText } from "@/lib/api";
import { addCall, DIRECTION_LABEL, type Direction } from "@/lib/calls-api";
import { readPart } from "@/lib/part";

/** 새 통화 추가 — 방향(걸려 온 전화가 기본)·전화번호(선택)·통화 내용. 끝나면 그 통화 상세를 연다
 * (통화·② 판정 화면 설계 "새 통화 추가") */
export function AddCallDialog({ onAdded }: { onAdded: (uid: string) => void }) {
  const [open, setOpen] = useState(false);
  const [direction, setDirection] = useState<Direction>("inbound");
  const [phone, setPhone] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { call_uid } = await addCall({
        direction,
        transcript: text,
        phone: phone.trim() || null,
        part: readPart(),
      });
      setOpen(false);
      setText("");
      setPhone("");
      setDirection("inbound");
      onAdded(call_uid);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !busy && setOpen(v)}>
      <DialogTrigger asChild>
        <Button>＋ 새 통화 추가</Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-xl">
        <form onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>새 통화 추가</DialogTitle>
            <DialogDescription>
              통화 내용을 글로 붙여 넣으면 AI가 요약·분류·할 일을 붙입니다.
            </DialogDescription>
          </DialogHeader>
          <div className="flex gap-2" role="radiogroup" aria-label="방향">
            {(["inbound", "outbound"] as const).map((d) => (
              <Button
                key={d}
                type="button"
                size="sm"
                role="radio"
                aria-checked={direction === d}
                variant={direction === d ? "secondary" : "outline"}
                onClick={() => setDirection(d)}
              >
                {DIRECTION_LABEL[d]}
              </Button>
            ))}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="add-phone">전화번호 (선택)</Label>
            <Input
              id="add-phone"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="010-0000-0000 — 환자와 맞으면 차트에 연결"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="add-text">통화 내용</Label>
            <Textarea
              id="add-text"
              rows={10}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={
                "직원: 클로드치과입니다.\n환자: 다음 주 화요일 스케일링 예약했는데 수요일로 바꿀 수 있을까요?"
              }
            />
          </div>
          {error && <p className="text-sm text-status-danger-fg">{error}</p>}
          <DialogFooter>
            <Button type="submit" disabled={busy || !text.trim()}>
              {busy ? "분류하는 중…" : "추가하기"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
