"""Command-line entry point for the offline coverage checker."""

import argparse
from datetime import date
from pathlib import Path

from checker import (
    DEFAULT_REQUIRED_TAGS,
    InputError,
    check_coverage,
    load_inputs,
    parse_date,
    write_reports,
)


def _as_of(value: str) -> date:
    try:
        return parse_date(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "유효한 YYYY-MM-DD 날짜를 입력하세요."
        ) from None


def _positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("점검 기한은 양의 정수여야 합니다.") from None
    if days < 1:
        raise argparse.ArgumentTypeError("점검 기한은 1일 이상이어야 합니다.")
    return days


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="CSV를 기준으로 점검 대상 누락·점검 기한 초과·예외 만료를 확인합니다."
    )
    parser.add_argument(
        "--as-of",
        type=_as_of,
        default=date.today(),
        help="기준일 YYYY-MM-DD (기본: 로컬 오늘)",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=root / "data", help="세 입력 CSV가 있는 폴더"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=root / "reports", help="보고서 저장 폴더"
    )
    parser.add_argument(
        "--stale-after-days",
        type=_positive_days,
        default=30,
        help="마지막 점검 성공일부터의 허용 기간 (기본: 30일)",
    )
    parser.add_argument(
        "--required-tags",
        default=",".join(DEFAULT_REQUIRED_TAGS),
        help="필수 태그 키를 쉼표로 구분",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="기존 summary.md와 findings.csv 덮어쓰기",
    )
    args = parser.parse_args(argv)
    try:
        assets, schedules, exceptions = load_inputs(args.data_dir)
        report = check_coverage(
            assets,
            schedules,
            exceptions,
            as_of=args.as_of,
            stale_after_days=args.stale_after_days,
            required_tags=tuple(key.strip() for key in args.required_tags.split(",")),
        )
        input_paths = [
            (args.data_dir / name).resolve()
            for name in ("assets.csv", "schedules.csv", "exceptions.csv")
        ]
        output_paths = [
            (args.output_dir / name).resolve()
            for name in ("summary.md", "findings.csv")
        ]
        if set(input_paths) & set(output_paths):
            raise InputError("입력 파일과 보고서 경로가 겹칩니다.")
        write_reports(report, args.output_dir, overwrite=args.overwrite)
    except InputError as error:
        print(f"오류: {error}")
        return 2
    except OSError:
        print(
            "오류: 보고서를 저장할 수 없습니다. 출력 폴더의 권한과 경로를 확인하세요. 일부 파일이 작성되었을 수 있습니다."
        )
        return 2
    print(
        f"전체 {len(report.results)}대 / 기간 내 점검 성공 {report.recent_success_count}대 / "
        f"정상 {report.count_status('정상')}대 / 예외 적용 {report.count_status('예외 적용')}대 / "
        f"확인 필요 {report.count_status('확인 필요')}대"
    )
    print("보고서 생성: summary.md, findings.csv")
    return 1 if report.count_status("확인 필요") else 0


if __name__ == "__main__":
    raise SystemExit(main())
