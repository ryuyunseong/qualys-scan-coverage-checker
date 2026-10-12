"""Tkinter interface for the local CSV coverage checker."""

from __future__ import annotations

import sys
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from checker import (
    DEFAULT_REQUIRED_TAGS,
    EXCEPTION_STATE_LABELS,
    SCAN_STATUS_LABELS,
    AssetCoverage,
    CoverageReport,
    InputError,
    check_coverage,
    load_inputs,
    parse_date,
    write_reports,
)

ROOT = Path(__file__).resolve().parent
SAMPLE_DATE = "2026-10-13"
FILTERS = ("전체", "확인 필요", "예외 적용", "정상")


def review_csv(
    data_dir: str, as_of: str, days: str, required_tags: str
) -> CoverageReport:
    """Validate form values and use the same checker as the CLI."""
    if not data_dir.strip():
        raise InputError("입력 CSV 폴더를 선택하세요.")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in data_dir):
        raise InputError("입력 폴더 경로에 제어 문자를 사용할 수 없습니다.")
    try:
        check_date = parse_date(as_of.strip())
    except ValueError:
        raise InputError("기준일은 유효한 YYYY-MM-DD 날짜로 입력하세요.") from None
    try:
        period = int(days.strip())
    except ValueError:
        raise InputError("점검 기한은 양의 정수로 입력하세요.") from None
    if period < 1:
        raise InputError("점검 기한은 1일 이상이어야 합니다.")
    return check_coverage(
        *load_inputs(Path(data_dir.strip())),
        as_of=check_date,
        stale_after_days=period,
        required_tags=tuple(key.strip() for key in required_tags.split(",")),
    )


def save_review(
    report: CoverageReport, data_dir: Path, output_dir: Path, *, overwrite: bool
) -> None:
    input_paths = {
        (data_dir / name).resolve()
        for name in ("assets.csv", "schedules.csv", "exceptions.csv")
    }
    output_paths = {
        (output_dir / name).resolve() for name in ("summary.md", "findings.csv")
    }
    if input_paths & output_paths:
        raise InputError("입력 파일과 보고서 경로가 겹칩니다.")
    write_reports(report, output_dir, overwrite=overwrite)


class CoverageApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.report: CoverageReport | None = None
        self.reviewed_data_dir: Path | None = None
        self.data_dir = tk.StringVar(root, str(ROOT / "data"))
        self.as_of = tk.StringVar(root, date.today().isoformat())
        self.days = tk.StringVar(root, "30")
        self.required_tags = tk.StringVar(root, ",".join(DEFAULT_REQUIRED_TAGS))
        self.filter = tk.StringVar(root, "전체")
        self.status = tk.StringVar(root, "입력 조건을 확인한 뒤 검토를 실행하세요.")
        self.totals = [tk.StringVar(root, "—") for _ in range(5)]
        self._build_window()
        for variable in (self.data_dir, self.as_of, self.days, self.required_tags):
            variable.trace_add("write", self._invalidate)
        self.filter.trace_add("write", self._render_results)

    def _build_window(self) -> None:
        self.root.title("Scan Coverage Checker — 점검 대상 및 이력 검토")
        scale = self.root.winfo_fpixels("1i") / 96
        width = min(round(1220 * scale), self.root.winfo_screenwidth() - 80)
        height = min(round(800 * scale), self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(980, width), min(700, height))
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Coverage.Title.TLabel", font=("맑은 고딕", 19, "bold"))
        style.configure("Coverage.Note.TLabel", foreground="#52637a")
        style.configure("Coverage.Number.TLabel", font=("맑은 고딕", 23, "bold"))
        style.configure("Coverage.Treeview", rowheight=30)

        container = ttk.Frame(self.root, padding=20)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(4, weight=1)
        ttk.Label(
            container, text="점검 대상 및 이력 검토", style="Coverage.Title.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            container,
            text="CSV 기준 검토 · 저장소 예제는 가상 데이터 · Qualys 접속 및 취약점 스캔 없음",
            style="Coverage.Note.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 16))

        settings = ttk.LabelFrame(container, text="검토 조건", padding=12)
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="입력 CSV 폴더").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.data_dir).grid(
            row=0, column=1, sticky="ew", padx=(12, 8)
        )
        ttk.Button(settings, text="폴더 선택", command=self._choose_input).grid(
            row=0, column=2
        )

        fields = ttk.Frame(settings)
        fields.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Label(fields, text="기준일").grid(row=0, column=0, sticky="w")
        ttk.Entry(fields, textvariable=self.as_of, width=14).grid(
            row=0, column=1, padx=(8, 20)
        )
        ttk.Label(fields, text="점검 기한(일)").grid(row=0, column=2)
        ttk.Entry(fields, textvariable=self.days, width=6).grid(
            row=0, column=3, padx=(8, 20)
        )
        ttk.Label(fields, text="필수 태그 키").grid(row=0, column=4)
        ttk.Entry(fields, textvariable=self.required_tags, width=27).grid(
            row=0, column=5, sticky="ew", padx=(8, 0)
        )
        fields.columnconfigure(5, weight=1)

        actions = ttk.Frame(settings)
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="예제 실행", command=self.run_example).pack(
            side="left"
        )
        self.run_button = ttk.Button(actions, text="검토 실행", command=self.run_review)
        self.run_button.pack(side="left", padx=8)
        ttk.Label(
            actions,
            text="예제 기준일: 2026-10-13 · 입력 파일은 수정하지 않습니다.",
            style="Coverage.Note.TLabel",
        ).pack(side="left", padx=8)

        counts = ttk.Frame(container)
        counts.grid(row=3, column=0, sticky="ew", pady=16)
        for column, label in enumerate(
            ("전체 자산", "기간 내 점검 성공", "정상", "예외 적용", "확인 필요")
        ):
            counts.columnconfigure(column, weight=1, uniform="count")
            card = ttk.LabelFrame(counts, text=label, padding=(12, 6))
            card.grid(
                row=0, column=column, sticky="ew", padx=(0, 8 if column < 4 else 0)
            )
            ttk.Label(
                card, textvariable=self.totals[column], style="Coverage.Number.TLabel"
            ).pack(anchor="w")

        results = ttk.Frame(container)
        results.grid(row=4, column=0, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(results)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text="자산별 결과").pack(side="left")
        ttk.Combobox(
            toolbar,
            textvariable=self.filter,
            values=FILTERS,
            state="readonly",
            width=12,
        ).pack(side="right")
        ttk.Label(toolbar, text="판정 필터").pack(side="right", padx=8)

        table = ttk.Frame(results)
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = (
            "asset_id",
            "hostname",
            "status",
            "schedules",
            "success_date",
            "latest",
            "exception",
        )
        self.tree = ttk.Treeview(
            table,
            columns=columns,
            show="headings",
            selectmode="browse",
            style="Coverage.Treeview",
            height=9,
        )
        headings = (
            ("자산 ID", 90),
            ("자산명", 160),
            ("종합 판정", 110),
            ("대상 스케줄", 180),
            ("마지막 점검 성공일", 155),
            ("최근 점검 결과", 125),
            ("예외 상태", 110),
        )
        for column, (label, width) in zip(columns, headings, strict=True):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=width, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.tag_configure("확인 필요", background="#fff4dc")
        self.tree.tag_configure("예외 적용", background="#edf3fc")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected)

        details = ttk.LabelFrame(container, text="선택한 자산의 상세 내용", padding=8)
        details.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        details.columnconfigure(0, weight=1)
        self.details = tk.Text(
            details,
            height=5,
            wrap="word",
            state="disabled",
            relief="flat",
            font="TkDefaultFont",
            background="#f7f9fc",
            padx=8,
            pady=8,
        )
        self.details.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(details, orient="vertical", command=self.details.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.details.configure(yscrollcommand=scroll.set)
        footer = ttk.Frame(container)
        footer.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status, style="Coverage.Note.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.save_button = ttk.Button(
            footer, text="전체 결과 저장", command=self.save_results, state="disabled"
        )
        self.save_button.grid(row=0, column=1, padx=(8, 0))
        self._set_details("자산을 선택하면 태그와 확인 사항을 볼 수 있습니다.")

    def _set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _invalidate(self, *_args: str) -> None:
        self.report = None
        self.reviewed_data_dir = None
        self.save_button.configure(state="disabled")
        for variable in self.totals:
            variable.set("—")
        self._render_results()
        self.status.set("입력 조건이 변경되었습니다. 다시 검토해 주세요.")

    def _choose_input(self) -> None:
        folder = filedialog.askdirectory(
            parent=self.root, title="입력 CSV 폴더 선택", mustexist=True
        )
        if folder:
            self.data_dir.set(folder)

    def run_example(self) -> None:
        self.data_dir.set(str(ROOT / "data"))
        self.as_of.set(SAMPLE_DATE)
        self.days.set("30")
        self.required_tags.set(",".join(DEFAULT_REQUIRED_TAGS))
        self.filter.set("전체")
        self.run_review()

    def run_review(self) -> None:
        self._invalidate()
        self.status.set("CSV를 읽고 검토하고 있습니다.")
        self.run_button.configure(state="disabled")
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            report = review_csv(
                self.data_dir.get(),
                self.as_of.get(),
                self.days.get(),
                self.required_tags.get(),
            )
        except InputError as error:
            self.status.set("검토하지 못했습니다. 입력 조건을 확인하세요.")
            messagebox.showerror("입력 확인", str(error), parent=self.root)
            return
        finally:
            self.run_button.configure(state="normal")
            self.root.configure(cursor="")
        self.report = report
        self.reviewed_data_dir = Path(self.data_dir.get().strip())
        values = (
            len(report.results),
            report.recent_success_count,
            report.count_status("정상"),
            report.count_status("예외 적용"),
            report.count_status("확인 필요"),
        )
        for variable, value in zip(self.totals, values, strict=True):
            variable.set(str(value))
        self.save_button.configure(state="normal")
        self._render_results()

    def _render_results(self, *_args: str) -> None:
        self.tree.delete(*self.tree.get_children())
        self._set_details("자산을 선택하면 태그와 확인 사항을 볼 수 있습니다.")
        if self.report is None:
            return
        results = [
            result
            for result in self.report.results
            if self.filter.get() == "전체" or result.status == self.filter.get()
        ]
        for result in results:
            asset = result.asset
            self.tree.insert(
                "",
                "end",
                iid=asset.asset_id,
                values=(
                    asset.asset_id,
                    asset.hostname,
                    result.status,
                    ", ".join(result.schedule_ids) or "—",
                    asset.last_successful_scan_date.isoformat()
                    if asset.last_successful_scan_date
                    else "—",
                    SCAN_STATUS_LABELS[asset.latest_scan_status],
                    EXCEPTION_STATE_LABELS[result.exception_state],
                ),
                tags=(result.status,),
            )
        self.status.set(
            f"검토 완료 · 기준일 {self.report.as_of.isoformat()} · 표시 {len(results)} / 전체 {len(self.report.results)}대"
        )

    def _show_selected(self, _event: tk.Event | None = None) -> None:
        selected = self.tree.selection()
        if self.report is None or not selected:
            return
        result: AssetCoverage = next(
            item for item in self.report.results if item.asset.asset_id == selected[0]
        )
        lines = [
            f"{result.asset.hostname} ({result.asset.asset_id}) · {result.status}",
            "태그: " + ("; ".join(sorted(result.asset.tags)) or "없음"),
            "예외 ID: " + (result.exception_id or "없음"),
        ]
        if result.findings:
            lines.extend(
                "• " + finding.detail + (" [예외 적용]" if finding.waived else "")
                for finding in result.findings
            )
        else:
            lines.append(
                "확인 사항 없음. 점검 대상에 포함되며 기한 내 성공 이력이 있습니다."
            )
        self._set_details("\n".join(lines))

    def save_results(self) -> None:
        if self.report is None or self.reviewed_data_dir is None:
            messagebox.showinfo(
                "검토 필요", "검토를 실행한 뒤 결과를 저장하세요.", parent=self.root
            )
            return
        folder = filedialog.askdirectory(
            parent=self.root,
            title="전체 결과 저장 폴더 선택",
            initialdir=str(ROOT / "reports"),
            mustexist=False,
        )
        if not folder:
            return
        output = Path(folder)
        overwrite = any(
            (output / name).exists() for name in ("summary.md", "findings.csv")
        )
        if overwrite and not messagebox.askyesno(
            "보고서 덮어쓰기",
            "선택한 폴더에 기존 보고서가 있습니다. summary.md와 findings.csv를 덮어쓰시겠습니까?",
            parent=self.root,
        ):
            return
        try:
            save_review(
                self.report, self.reviewed_data_dir, output, overwrite=overwrite
            )
        except InputError as error:
            messagebox.showerror("저장 확인", str(error), parent=self.root)
            return
        except OSError:
            messagebox.showerror(
                "저장 오류",
                "보고서를 저장할 수 없습니다. 폴더 권한과 경로를 확인하세요. 일부 파일이 작성되었을 수 있습니다.",
                parent=self.root,
            )
            return
        self.status.set("전체 결과 저장 완료 · summary.md, findings.csv")
        messagebox.showinfo(
            "저장 완료",
            "전체 자산의 summary.md와 findings.csv를 저장했습니다.",
            parent=self.root,
        )


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError:
        print(
            "GUI를 시작할 수 없습니다. 데스크톱 환경과 Python의 Tkinter 설치를 확인하세요.",
            file=sys.stderr,
        )
        return 2
    CoverageApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
