"""Synthetic, offline scan coverage checks using only the Python standard library."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_REQUIRED_TAGS = ("region", "env", "role")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
TAG_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
WAIVABLE_CODES = frozenset({"NO_SCHEDULE", "SCAN_NEVER", "SCAN_STALE"})
SCAN_STATUS_LABELS = {"success": "성공", "failed": "실패", "never": "미실행"}
EXCEPTION_STATE_LABELS = {
    "none": "없음",
    "active": "유효",
    "expired": "만료",
    "pending": "승인 대기",
    "rejected": "반려",
    "not_started": "적용 전",
}


class InputError(ValueError):
    """An invalid input, with a message that does not include raw field values."""


@dataclass(frozen=True)
class Asset:
    asset_id: str
    hostname: str
    tags: frozenset[str]
    last_successful_scan_date: date | None
    latest_scan_status: str


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    enabled: bool
    include_tags: frozenset[str]
    include_match: str
    exclude_tags: frozenset[str]
    exclude_match: str

    def matches(self, tags: frozenset[str]) -> bool:
        return (
            self.enabled
            and _matches_tags(tags, self.include_tags, self.include_match)
            and not _matches_tags(tags, self.exclude_tags, self.exclude_match)
        )


@dataclass(frozen=True)
class ScanException:
    exception_id: str
    asset_id: str
    approval_status: str
    starts_on: date
    expires_on: date

    def is_active(self, as_of: date) -> bool:
        return (
            self.approval_status == "approved"
            and self.starts_on <= as_of <= self.expires_on
        )


@dataclass(frozen=True)
class Finding:
    code: str
    detail: str
    waived: bool = False


@dataclass(frozen=True)
class AssetCoverage:
    asset: Asset
    schedule_ids: tuple[str, ...]
    exception_id: str | None
    exception_state: str
    scan_age_days: int | None
    findings: tuple[Finding, ...]

    @property
    def status(self) -> str:
        if any(not finding.waived for finding in self.findings):
            return "확인 필요"
        if any(finding.waived for finding in self.findings):
            return "예외 적용"
        return "정상"


@dataclass(frozen=True)
class CoverageReport:
    as_of: date
    stale_after_days: int
    required_tags: tuple[str, ...]
    results: tuple[AssetCoverage, ...]

    @property
    def recent_success_count(self) -> int:
        return sum(
            result.scan_age_days is not None
            and result.scan_age_days <= self.stale_after_days
            for result in self.results
        )

    def count_status(self, status: str) -> int:
        return sum(result.status == status for result in self.results)


def parse_date(value: str) -> date:
    """Accept a calendar date written exactly as YYYY-MM-DD."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    return date.fromisoformat(value)


def _invalid(label: str, line: int, field: str, explanation: str) -> InputError:
    return InputError(f"{label} {line}행: {field} {explanation}")


def _read_csv(path: Path, fields: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    rows = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            headers = reader.fieldnames
            if (
                headers is None
                or len(headers) != len(set(headers))
                or set(headers) != set(fields)
            ):
                raise InputError(f"{path.name}: CSV 열 구성이 올바르지 않습니다.")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise InputError(
                        f"{path.name} {reader.line_num}행: 열 수가 다릅니다."
                    )
                if any(
                    any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
                    for value in row.values()
                ):
                    raise InputError(
                        f"{path.name} {reader.line_num}행: 제어 문자는 허용하지 않습니다."
                    )
                rows.append(
                    (
                        reader.line_num,
                        {key: value.strip() for key, value in row.items()},
                    )
                )
    except (OSError, UnicodeError, csv.Error):
        raise InputError(f"{path.name}: UTF-8 CSV 파일을 읽을 수 없습니다.") from None
    return rows


def _identifier(value: str, label: str, line: int, field: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise _invalid(label, line, field, "형식이 올바르지 않습니다.")
    return value


def _choice(value: str, choices: set[str], label: str, line: int, field: str) -> str:
    if value not in choices:
        raise _invalid(label, line, field, "값이 허용된 목록에 없습니다.")
    return value


def _date(value: str, label: str, line: int, field: str) -> date:
    try:
        return parse_date(value)
    except ValueError:
        raise _invalid(
            label, line, field, "값은 유효한 YYYY-MM-DD 날짜여야 합니다."
        ) from None


def _tags(value: str, label: str, line: int, field: str) -> frozenset[str]:
    if not value:
        return frozenset()
    tags = [tag.strip() for tag in value.split(";")]
    for tag in tags:
        key, separator, tag_value = tag.partition(":")
        if (
            not separator
            or not TAG_KEY.fullmatch(key)
            or not IDENTIFIER.fullmatch(tag_value)
        ):
            raise _invalid(
                label, line, field, "값은 key:value 태그를 ;로 구분해야 합니다."
            )
    if len(tags) != len(set(tags)):
        raise _invalid(label, line, field, "값에 중복 태그가 있습니다.")
    return frozenset(tags)


def _unique(value: str, seen: set[str], label: str, line: int, field: str) -> None:
    if value in seen:
        raise _invalid(label, line, field, "값이 중복되었습니다.")
    seen.add(value)


def load_inputs(
    data_dir: Path,
) -> tuple[list[Asset], list[Schedule], list[ScanException]]:
    """Load the three local snapshot files; reject ambiguous or malformed rows."""
    assets = []
    asset_ids: set[str] = set()
    fields = (
        "asset_id",
        "hostname",
        "tags",
        "last_successful_scan_date",
        "latest_scan_status",
    )
    for line, row in _read_csv(data_dir / "assets.csv", fields):
        label = "assets.csv"
        asset_id = _identifier(row["asset_id"], label, line, "asset_id")
        _unique(asset_id, asset_ids, label, line, "asset_id")
        hostname = _identifier(row["hostname"], label, line, "hostname")
        tags = _tags(row["tags"], label, line, "tags")
        keys = [tag.partition(":")[0] for tag in tags]
        if len(keys) != len(set(keys)):
            raise _invalid(
                label, line, "tags", "자산에는 키별로 하나의 값만 허용합니다."
            )
        scan_date = (
            _date(
                row["last_successful_scan_date"],
                label,
                line,
                "last_successful_scan_date",
            )
            if row["last_successful_scan_date"]
            else None
        )
        status = _choice(
            row["latest_scan_status"],
            {"success", "failed", "never"},
            label,
            line,
            "latest_scan_status",
        )
        if (status == "success" and scan_date is None) or (
            status == "never" and scan_date is not None
        ):
            raise _invalid(
                label, line, "latest_scan_status", "성공 이력과 일치하지 않습니다."
            )
        assets.append(Asset(asset_id, hostname, tags, scan_date, status))
    if not assets:
        raise InputError("assets.csv: 자산이 한 개 이상 필요합니다.")

    schedules = []
    schedule_ids: set[str] = set()
    fields = (
        "schedule_id",
        "enabled",
        "include_tags",
        "include_match",
        "exclude_tags",
        "exclude_match",
    )
    for line, row in _read_csv(data_dir / "schedules.csv", fields):
        label = "schedules.csv"
        schedule_id = _identifier(row["schedule_id"], label, line, "schedule_id")
        _unique(schedule_id, schedule_ids, label, line, "schedule_id")
        enabled = (
            _choice(row["enabled"], {"true", "false"}, label, line, "enabled") == "true"
        )
        include_tags = _tags(row["include_tags"], label, line, "include_tags")
        if not include_tags:
            raise _invalid(label, line, "include_tags", "태그가 한 개 이상 필요합니다.")
        include_match = _choice(
            row["include_match"], {"ANY", "ALL"}, label, line, "include_match"
        )
        exclude_tags = _tags(row["exclude_tags"], label, line, "exclude_tags")
        exclude_match = _choice(
            row["exclude_match"], {"ANY", "ALL"}, label, line, "exclude_match"
        )
        schedules.append(
            Schedule(
                schedule_id,
                enabled,
                include_tags,
                include_match,
                exclude_tags,
                exclude_match,
            )
        )

    exceptions = []
    exception_ids: set[str] = set()
    exception_assets: set[str] = set()
    fields = ("exception_id", "asset_id", "approval_status", "starts_on", "expires_on")
    for line, row in _read_csv(data_dir / "exceptions.csv", fields):
        label = "exceptions.csv"
        exception_id = _identifier(row["exception_id"], label, line, "exception_id")
        _unique(exception_id, exception_ids, label, line, "exception_id")
        asset_id = _identifier(row["asset_id"], label, line, "asset_id")
        if asset_id not in asset_ids:
            raise _invalid(label, line, "asset_id", "자산 목록에 없는 참조입니다.")
        _unique(asset_id, exception_assets, label, line, "asset_id")
        approval = _choice(
            row["approval_status"],
            {"approved", "pending", "rejected"},
            label,
            line,
            "approval_status",
        )
        starts_on = _date(row["starts_on"], label, line, "starts_on")
        expires_on = _date(row["expires_on"], label, line, "expires_on")
        if starts_on > expires_on:
            raise _invalid(label, line, "expires_on", "시작일보다 빠를 수 없습니다.")
        exceptions.append(
            ScanException(exception_id, asset_id, approval, starts_on, expires_on)
        )
    return assets, schedules, exceptions


def _matches_tags(tags: frozenset[str], selected: frozenset[str], mode: str) -> bool:
    if not selected:
        return False
    return selected <= tags if mode == "ALL" else bool(selected & tags)


def check_coverage(
    assets: list[Asset],
    schedules: list[Schedule],
    exceptions: list[ScanException],
    *,
    as_of: date,
    stale_after_days: int = 30,
    required_tags: tuple[str, ...] = DEFAULT_REQUIRED_TAGS,
) -> CoverageReport:
    if stale_after_days < 1:
        raise InputError("점검 기한은 1일 이상이어야 합니다.")
    if (
        not required_tags
        or len(required_tags) != len(set(required_tags))
        or any(not TAG_KEY.fullmatch(key) for key in required_tags)
    ):
        raise InputError("필수 태그 키 목록이 올바르지 않습니다.")
    by_asset = {exception.asset_id: exception for exception in exceptions}
    results = []
    for asset in sorted(assets, key=lambda item: item.asset_id):
        findings = []
        keys = {tag.partition(":")[0] for tag in asset.tags}
        missing = sorted(set(required_tags) - keys)
        if missing:
            findings.append(
                Finding("MISSING_TAGS", "필수 태그 누락: " + ", ".join(missing))
            )
        schedule_ids = tuple(
            sorted(
                schedule.schedule_id
                for schedule in schedules
                if schedule.matches(asset.tags)
            )
        )
        if not schedule_ids:
            findings.append(
                Finding("NO_SCHEDULE", "활성 스케줄의 점검 대상에 포함되지 않음")
            )

        scan_age = None
        if asset.last_successful_scan_date is None:
            findings.append(Finding("SCAN_NEVER", "점검 성공 이력 없음"))
        else:
            scan_age = (as_of - asset.last_successful_scan_date).days
            if scan_age < 0:
                raise InputError("assets.csv: 기준일 이후의 성공 점검일이 있습니다.")
            if scan_age > stale_after_days:
                findings.append(
                    Finding(
                        "SCAN_STALE",
                        f"점검 기한 초과: 마지막 성공일로부터 {scan_age}일 경과 (기준 {stale_after_days}일)",
                    )
                )
        if asset.latest_scan_status == "failed":
            findings.append(
                Finding(
                    "LATEST_SCAN_FAILED",
                    "최근 점검 실패",
                )
            )

        exception = by_asset.get(asset.asset_id)
        exception_state = "none"
        if exception is not None:
            if exception.is_active(as_of):
                exception_state = "active"
                findings = [
                    Finding(
                        finding.code, finding.detail, finding.code in WAIVABLE_CODES
                    )
                    for finding in findings
                ]
            elif exception.approval_status != "approved":
                exception_state = exception.approval_status
                findings.append(
                    Finding(
                        "EXCEPTION_UNAPPROVED",
                        "예외 " + EXCEPTION_STATE_LABELS[exception_state],
                    )
                )
            elif as_of > exception.expires_on:
                exception_state = "expired"
                findings.append(
                    Finding(
                        "EXCEPTION_EXPIRED",
                        f"예외 만료: {exception.expires_on.isoformat()}",
                    )
                )
            else:
                exception_state = "not_started"
                findings.append(
                    Finding(
                        "EXCEPTION_NOT_STARTED",
                        f"예외 적용 전: 시작일 {exception.starts_on.isoformat()}",
                    )
                )
        results.append(
            AssetCoverage(
                asset,
                schedule_ids,
                exception.exception_id if exception else None,
                exception_state,
                scan_age,
                tuple(findings),
            )
        )
    return CoverageReport(as_of, stale_after_days, required_tags, tuple(results))


def render_summary(report: CoverageReport) -> str:
    lines = [
        "# 점검 대상 및 이력 검토 보고서",
        "",
        "입력 CSV의 자산·스케줄·점검 이력을 검토한 결과입니다. 이 도구는 Qualys API나 스캐너를 호출하지 않습니다.",
        "",
        f"- 기준일: {report.as_of.isoformat()}",
        f"- 점검 기한: {report.stale_after_days}일 (마지막 성공일 기준)",
        f"- 필수 태그 키: {', '.join(report.required_tags)}",
        "",
        "| 전체 자산 | 기간 내 점검 성공 | 정상 | 예외 적용 | 확인 필요 |",
        "| ---: | ---: | ---: | ---: | ---: |",
        f"| {len(report.results)} | {report.recent_success_count} | {report.count_status('정상')} | {report.count_status('예외 적용')} | {report.count_status('확인 필요')} |",
        "",
        "기간 내 점검 성공은 마지막 성공일이 점검 기한 이내인 자산 수입니다. 스케줄 누락이나 최근 점검 실패가 있으면 성공 이력이 최근이어도 확인 대상으로 분류합니다.",
        "",
        "| 자산 | 대상 스케줄 | 마지막 점검 성공일 | 최근 점검 결과 | 예외 상태 | 확인 사항 | 종합 판정 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in report.results:
        asset = result.asset
        details = (
            "; ".join(
                finding.detail + (" [예외 적용]" if finding.waived else "")
                for finding in result.findings
            )
            or "점검 대상 포함, 기간 내 성공 이력 확인"
        )
        lines.append(
            f"| {asset.hostname} | {', '.join(result.schedule_ids) or '-'} | "
            f"{asset.last_successful_scan_date.isoformat() if asset.last_successful_scan_date else '-'} | "
            f"{SCAN_STATUS_LABELS[asset.latest_scan_status]} | {EXCEPTION_STATE_LABELS[result.exception_state]} | {details} | {result.status} |"
        )
    lines.extend(
        [
            "",
            "유효한 승인 예외는 스케줄 누락·성공 이력 없음·점검 기한 초과에만 적용합니다. 필수 태그 누락과 최근 점검 실패는 예외 대상에서 제외합니다. 예외는 만료일 당일까지 유효합니다.",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(
    report: CoverageReport, output_dir: Path, *, overwrite: bool = False
) -> None:
    """Write fixed report names; refuse implicit overwrites and symlink targets."""
    summary_path = output_dir / "summary.md"
    findings_path = output_dir / "findings.csv"
    for path in (summary_path, findings_path):
        if path.is_symlink():
            raise InputError("보고서 경로에 심볼릭 링크 파일을 사용할 수 없습니다.")
        if path.exists() and (not overwrite or not path.is_file()):
            raise InputError(
                "보고서가 이미 있거나 출력 경로가 올바르지 않습니다. 새 --output-dir 또는 --overwrite를 사용하세요."
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    with findings_path.open(mode, encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            (
                "as_of",
                "asset_id",
                "hostname",
                "status",
                "finding_code",
                "detail",
                "disposition",
                "schedule_ids",
                "exception_id",
                "report_type",
            )
        )
        for result in report.results:
            for finding in result.findings:
                writer.writerow(
                    (
                        report.as_of.isoformat(),
                        result.asset.asset_id,
                        result.asset.hostname,
                        result.status,
                        finding.code,
                        finding.detail,
                        "waived" if finding.waived else "review",
                        ";".join(result.schedule_ids),
                        result.exception_id or "",
                        "scan_coverage_check",
                    )
                )
    with summary_path.open(mode, encoding="utf-8", newline="\n") as handle:
        handle.write(render_summary(report))
