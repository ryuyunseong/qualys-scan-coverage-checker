import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from checker import (
    Asset,
    InputError,
    ScanException,
    Schedule,
    check_coverage,
    load_inputs,
    parse_date,
    render_summary,
    write_reports,
)
from main import main

AS_OF = date(2026, 10, 13)
ROOT = Path(__file__).resolve().parents[1]
TAGS = frozenset({"region:KR", "env:prod", "role:web"})


def asset(**changes):
    return replace(Asset("A001", "KR-WEB-01", TAGS, AS_OF, "success"), **changes)


def schedule(**changes):
    return replace(
        Schedule("SCH001", True, frozenset({"region:KR"}), "ANY", frozenset(), "ANY"),
        **changes,
    )


def exception(**changes):
    return replace(
        ScanException("EX001", "A001", "approved", AS_OF - timedelta(days=10), AS_OF),
        **changes,
    )


class CoverageTests(unittest.TestCase):
    def check_one(self, target=None, schedules=None, exceptions=None, **options):
        report = check_coverage(
            [target or asset()],
            [schedule()] if schedules is None else schedules,
            [] if exceptions is None else exceptions,
            as_of=AS_OF,
            **options,
        )
        return report.results[0]

    def test_recent_success_on_enabled_schedule_is_normal(self):
        result = self.check_one()
        self.assertEqual(result.status, "정상")
        self.assertEqual(result.findings, ())

    def test_valid_exception_without_waived_findings_keeps_normal_status(self):
        report = check_coverage([asset()], [schedule()], [exception()], as_of=AS_OF)
        result = report.results[0]
        self.assertEqual(result.exception_state, "active")
        self.assertEqual(result.findings, ())
        self.assertEqual(result.status, "정상")
        self.assertEqual(report.count_status("예외 적용"), 0)
        self.assertEqual(report.recent_success_count, 1)

    def test_30_days_is_valid_but_31_days_is_stale(self):
        for days, expected in ((30, "정상"), (31, "확인 필요")):
            with self.subTest(days=days):
                result = self.check_one(
                    asset(last_successful_scan_date=AS_OF - timedelta(days=days))
                )
                self.assertEqual(result.status, expected)

    def test_custom_scan_period_and_required_tag(self):
        result = self.check_one(
            asset(last_successful_scan_date=AS_OF - timedelta(days=8)),
            stale_after_days=7,
            required_tags=("owner",),
        )
        self.assertEqual(
            {f.code for f in result.findings}, {"SCAN_STALE", "MISSING_TAGS"}
        )

    def test_missing_tags_does_not_imply_schedule_missing(self):
        result = self.check_one(asset(tags=frozenset({"region:KR", "role:web"})))
        self.assertEqual([f.code for f in result.findings], ["MISSING_TAGS"])
        self.assertEqual(result.schedule_ids, ("SCH001",))

    def test_disabled_schedule_does_not_cover_asset(self):
        result = self.check_one(schedules=[schedule(enabled=False)])
        self.assertEqual([f.code for f in result.findings], ["NO_SCHEDULE"])

    def test_any_and_all_include_selection(self):
        selected = frozenset({"region:KR", "role:db"})
        for mode, covered in (("ANY", True), ("ALL", False)):
            with self.subTest(mode=mode):
                result = self.check_one(
                    schedules=[schedule(include_tags=selected, include_match=mode)]
                )
                self.assertEqual(bool(result.schedule_ids), covered)

    def test_exclusion_overrides_inclusion_with_any_or_all(self):
        selected = frozenset({"region:KR", "role:db"})
        for mode, covered in (("ANY", False), ("ALL", True)):
            with self.subTest(mode=mode):
                result = self.check_one(
                    schedules=[schedule(exclude_tags=selected, exclude_match=mode)]
                )
                self.assertEqual(bool(result.schedule_ids), covered)
        result = self.check_one(
            schedules=[schedule(exclude_tags=TAGS, exclude_match="ALL")]
        )
        self.assertFalse(result.schedule_ids)

    def test_empty_exclusion_does_not_exclude_all_assets(self):
        result = self.check_one(schedules=[schedule(exclude_match="ALL")])
        self.assertEqual(result.status, "정상")

    def test_one_matching_schedule_is_enough(self):
        result = self.check_one(
            schedules=[schedule(enabled=False), schedule(schedule_id="SCH002")]
        )
        self.assertEqual(result.schedule_ids, ("SCH002",))
        self.assertEqual(result.status, "정상")

    def test_scheduled_asset_without_success_is_not_normal(self):
        result = self.check_one(
            asset(last_successful_scan_date=None, latest_scan_status="never")
        )
        self.assertEqual(result.schedule_ids, ("SCH001",))
        self.assertEqual([f.code for f in result.findings], ["SCAN_NEVER"])
        self.assertEqual(result.status, "확인 필요")

    def test_failed_latest_attempt_preserves_previous_success(self):
        report = check_coverage(
            [asset(latest_scan_status="failed")], [schedule()], [], as_of=AS_OF
        )
        self.assertEqual(report.recent_success_count, 1)
        self.assertEqual(report.results[0].status, "확인 필요")
        self.assertEqual(
            [f.code for f in report.results[0].findings], ["LATEST_SCAN_FAILED"]
        )

    def test_failed_without_success_reports_both_facts(self):
        result = self.check_one(
            asset(latest_scan_status="failed", last_successful_scan_date=None)
        )
        self.assertEqual(
            {f.code for f in result.findings}, {"SCAN_NEVER", "LATEST_SCAN_FAILED"}
        )

    def test_recent_success_does_not_mask_schedule_missing(self):
        report = check_coverage([asset()], [], [], as_of=AS_OF)
        self.assertEqual(report.recent_success_count, 1)
        self.assertEqual(report.count_status("확인 필요"), 1)

    def test_active_exception_keeps_waived_findings_visible(self):
        result = self.check_one(
            asset(last_successful_scan_date=None, latest_scan_status="never"),
            schedules=[],
            exceptions=[exception()],
        )
        self.assertEqual(result.status, "예외 적용")
        self.assertEqual(
            {f.code for f in result.findings}, {"NO_SCHEDULE", "SCAN_NEVER"}
        )
        self.assertTrue(all(f.waived for f in result.findings))
        self.assertEqual(result.schedule_ids, ())

    def test_active_exception_waives_stale_success_only(self):
        result = self.check_one(
            asset(last_successful_scan_date=AS_OF - timedelta(days=45)),
            exceptions=[exception()],
        )
        self.assertEqual(result.status, "예외 적용")
        self.assertTrue(result.findings[0].waived)

    def test_active_exception_does_not_waive_missing_tags_or_failure(self):
        result = self.check_one(
            asset(
                tags=frozenset(),
                latest_scan_status="failed",
                last_successful_scan_date=None,
            ),
            exceptions=[exception()],
        )
        self.assertEqual(result.status, "확인 필요")
        self.assertEqual(
            {f.code for f in result.findings if not f.waived},
            {"MISSING_TAGS", "LATEST_SCAN_FAILED"},
        )

    def test_exception_start_and_expiry_dates_are_inclusive(self):
        for ex in (exception(starts_on=AS_OF), exception(expires_on=AS_OF)):
            with self.subTest(exception=ex):
                result = self.check_one(schedules=[], exceptions=[ex])
                self.assertEqual(result.status, "예외 적용")

    def test_expired_future_pending_and_rejected_do_not_waive(self):
        cases = (
            (
                exception(expires_on=AS_OF - timedelta(days=1)),
                "EXCEPTION_EXPIRED",
                "expired",
            ),
            (
                exception(
                    starts_on=AS_OF + timedelta(days=1),
                    expires_on=AS_OF + timedelta(days=10),
                ),
                "EXCEPTION_NOT_STARTED",
                "not_started",
            ),
            (exception(approval_status="pending"), "EXCEPTION_UNAPPROVED", "pending"),
            (exception(approval_status="rejected"), "EXCEPTION_UNAPPROVED", "rejected"),
        )
        for ex, code, state in cases:
            with self.subTest(state=state):
                result = self.check_one(schedules=[], exceptions=[ex])
                self.assertEqual(result.exception_state, state)
                self.assertEqual(
                    {f.code for f in result.findings}, {"NO_SCHEDULE", code}
                )
                self.assertFalse(any(f.waived for f in result.findings))

    def test_expired_exception_is_visible_even_after_recent_success(self):
        result = self.check_one(
            exceptions=[exception(expires_on=AS_OF - timedelta(days=1))]
        )
        self.assertEqual([f.code for f in result.findings], ["EXCEPTION_EXPIRED"])

    def test_future_success_is_rejected_even_with_exception(self):
        with self.assertRaises(InputError):
            self.check_one(
                asset(last_successful_scan_date=AS_OF + timedelta(days=1)),
                exceptions=[exception()],
            )

    def test_invalid_policy_is_rejected(self):
        for options in (
            {"stale_after_days": 0},
            {"required_tags": ()},
            {"required_tags": ("env", "env")},
            {"required_tags": ("=bad",)},
        ):
            with self.subTest(options=options), self.assertRaises(InputError):
                self.check_one(**options)

    def test_sample_snapshot_has_expected_counts_and_45_day_finding(self):
        report = check_coverage(*load_inputs(ROOT / "data"), as_of=AS_OF)
        self.assertEqual(len(report.results), 12)
        self.assertEqual(report.recent_success_count, 8)
        self.assertEqual(report.count_status("정상"), 8)
        self.assertEqual(report.count_status("확인 필요"), 4)
        self.assertEqual(report.count_status("예외 적용"), 0)
        result = next(r for r in report.results if r.asset.asset_id == "A003")
        self.assertEqual(result.scan_age_days, 45)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        for name in ("assets.csv", "schedules.csv", "exceptions.csv"):
            (self.directory / name).write_bytes((ROOT / "data" / name).read_bytes())

    def change(self, name, field, value, row_index=0):
        path = self.directory / name
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames
            rows = list(reader)
        rows[row_index][field] = value
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_bom_input_is_supported(self):
        self.change("assets.csv", "hostname", "SYNTHETIC-01")
        self.assertEqual(load_inputs(self.directory)[0][0].hostname, "SYNTHETIC-01")

    def test_bad_assets_are_rejected_without_raw_values(self):
        cases = (
            ("asset_id", "=SECRET_SENTINEL"),
            ("hostname", "@SECRET_SENTINEL"),
            ("tags", "env:SECRET_SENTINEL;env:dev"),
            ("tags", "env:prod;env:prod;SECRET_SENTINEL"),
            ("tags", "env:"),
            ("tags", "env:prod;"),
            ("last_successful_scan_date", "2026-02-30"),
            ("last_successful_scan_date", "20261013"),
            ("latest_scan_status", "SECRET_SENTINEL"),
            ("last_successful_scan_date", ""),
        )
        original = (ROOT / "data" / "assets.csv").read_bytes()
        for field, value in cases:
            with self.subTest(field=field, value=value):
                (self.directory / "assets.csv").write_bytes(original)
                self.change("assets.csv", field, value)
                with self.assertRaises(InputError) as caught:
                    load_inputs(self.directory)
                self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_never_status_cannot_have_success_date(self):
        self.change("assets.csv", "latest_scan_status", "never")
        with self.assertRaises(InputError):
            load_inputs(self.directory)

    def test_duplicate_asset_ids_are_rejected(self):
        self.change("assets.csv", "asset_id", "A001", row_index=1)
        with self.assertRaises(InputError):
            load_inputs(self.directory)

    def test_bad_schedules_are_rejected(self):
        cases = (
            ("enabled", "yes"),
            ("include_match", "any"),
            ("exclude_match", "NONE"),
            ("include_tags", ""),
            ("exclude_tags", "invalid"),
        )
        original = (ROOT / "data" / "schedules.csv").read_bytes()
        for field, value in cases:
            with self.subTest(field=field):
                (self.directory / "schedules.csv").write_bytes(original)
                self.change("schedules.csv", field, value)
                with self.assertRaises(InputError):
                    load_inputs(self.directory)

    def test_duplicate_schedule_ids_are_rejected(self):
        self.change("schedules.csv", "schedule_id", "SCH-KR", row_index=1)
        with self.assertRaises(InputError):
            load_inputs(self.directory)

    def test_bad_exceptions_are_rejected(self):
        cases = (
            ("asset_id", "UNKNOWN"),
            ("approval_status", "yes"),
            ("starts_on", "2026-10-13"),
            ("expires_on", "2026-99-01"),
        )
        original = (ROOT / "data" / "exceptions.csv").read_bytes()
        for field, value in cases:
            with self.subTest(field=field):
                (self.directory / "exceptions.csv").write_bytes(original)
                self.change("exceptions.csv", field, value)
                with self.assertRaises(InputError):
                    load_inputs(self.directory)

    def test_duplicate_exception_or_asset_is_rejected(self):
        original = (ROOT / "data" / "exceptions.csv").read_text(encoding="utf-8")
        for row in (
            "EX001,A001,approved,2026-09-01,2026-10-12\n",
            "EX002,A005,approved,2026-09-01,2026-10-12\n",
        ):
            with self.subTest(row=row):
                (self.directory / "exceptions.csv").write_text(
                    original + row, encoding="utf-8"
                )
                with self.assertRaises(InputError):
                    load_inputs(self.directory)

    def test_bad_csv_structure_encoding_and_quotes_are_rejected(self):
        cases = (
            b"asset_id,hostname\nA001,HOST\n",
            b"asset_id,asset_id,tags,last_successful_scan_date,latest_scan_status\n",
            b"asset_id,hostname,tags,last_successful_scan_date,latest_scan_status\nA001,HOST\n",
            b"asset_id,hostname,tags,last_successful_scan_date,latest_scan_status\nA001,HOST,,2026-10-13,success,EXTRA\n",
            b'asset_id,hostname,tags,last_successful_scan_date,latest_scan_status\n"unclosed',
            b"\xff\xfeinvalid",
            b"",
        )
        for content in cases:
            with self.subTest(content=content):
                (self.directory / "assets.csv").write_bytes(content)
                with self.assertRaises(InputError):
                    load_inputs(self.directory)

    def test_embedded_control_character_is_rejected(self):
        self.change("assets.csv", "hostname", "HOST\nSECRET_SENTINEL")
        with self.assertRaises(InputError) as caught:
            load_inputs(self.directory)
        self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_control_characters_at_field_edges_are_rejected(self):
        for value in (
            "\tHOST",
            "HOST\t",
            "\nHOST",
            "HOST\r",
            "\vHOST",
            "HOST\x7f",
            "\u0085HOST",
            "HOST\u009f",
        ):
            with self.subTest(value=value):
                self.change("assets.csv", "hostname", value)
                with self.assertRaises(InputError):
                    load_inputs(self.directory)

    def test_spaces_at_field_edges_are_still_trimmed(self):
        self.change("assets.csv", "hostname", "  SYNTHETIC-01  ")
        self.assertEqual(load_inputs(self.directory)[0][0].hostname, "SYNTHETIC-01")

    def test_header_only_schedule_and_exception_files_are_valid(self):
        for name in ("schedules.csv", "exceptions.csv"):
            header = (self.directory / name).read_text(encoding="utf-8").splitlines()[0]
            (self.directory / name).write_text(header + "\n", encoding="utf-8")
        assets, schedules, exceptions = load_inputs(self.directory)
        self.assertEqual(len(assets), 12)
        self.assertEqual((schedules, exceptions), ([], []))

    def test_header_only_assets_are_rejected(self):
        header = (
            (self.directory / "assets.csv").read_text(encoding="utf-8").splitlines()[0]
        )
        (self.directory / "assets.csv").write_text(header + "\n", encoding="utf-8")
        with self.assertRaises(InputError):
            load_inputs(self.directory)

    def test_missing_files_use_sanitized_error(self):
        with self.assertRaises(InputError) as caught:
            load_inputs(self.directory / "SECRET_SENTINEL")
        self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_date_parser_requires_iso_calendar_date(self):
        self.assertEqual(parse_date("2024-02-29"), date(2024, 2, 29))
        for value in ("2026-02-29", "20261013", "2026-W42-2", "2026-1-13"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_date(value)


class ReportAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "reports"
        self.report = check_coverage(*load_inputs(ROOT / "data"), as_of=AS_OF)

    def test_csv_reports_each_finding_with_asset_identity(self):
        write_reports(self.report, self.output)
        with (self.output / "findings.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 7)
        self.assertEqual(
            {r["asset_id"] for r in rows}, {"A002", "A003", "A004", "A005"}
        )
        self.assertEqual({r["disposition"] for r in rows}, {"review"})
        self.assertEqual({r["report_type"] for r in rows}, {"scan_coverage_check"})
        self.assertIn(
            "| 12 | 8 | 8 | 0 | 4 |",
            (self.output / "summary.md").read_text(encoding="utf-8"),
        )

    def test_waived_findings_remain_in_csv_and_markdown(self):
        report = check_coverage(
            [asset(last_successful_scan_date=None, latest_scan_status="never")],
            [],
            [exception()],
            as_of=AS_OF,
        )
        write_reports(report, self.output)
        with (self.output / "findings.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["disposition"] for r in rows}, {"waived"})
        self.assertIn("[예외 적용]", render_summary(report))

    def test_no_findings_csv_contains_header_only(self):
        report = check_coverage([asset()], [schedule()], [], as_of=AS_OF)
        write_reports(report, self.output)
        with (self.output / "findings.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])

    def test_markdown_uses_readable_labels_and_identifies_coverage_review(self):
        text = render_summary(self.report)
        self.assertIn("점검 대상 및 이력 검토 보고서", text)
        self.assertIn("Qualys API나 스캐너를 호출하지 않습니다", text)
        self.assertIn("| 성공 | 없음 |", text)
        self.assertIn("| 미실행 | 없음 |", text)
        self.assertIn("| 성공 | 만료 |", text)
        self.assertNotIn("| success |", text)
        self.assertNotIn("| none |", text)
        self.assertIn("예외 만료: 2026-10-12", text)

    def test_existing_reports_require_explicit_overwrite(self):
        write_reports(self.report, self.output)
        original = (self.output / "summary.md").read_bytes()
        with self.assertRaises(InputError):
            write_reports(self.report, self.output)
        self.assertEqual((self.output / "summary.md").read_bytes(), original)
        write_reports(self.report, self.output, overwrite=True)
        self.assertEqual((self.output / "summary.md").read_bytes(), original)

    def test_existing_summary_does_not_create_partial_csv(self):
        self.output.mkdir()
        (self.output / "summary.md").write_text("keep", encoding="utf-8")
        with self.assertRaises(InputError):
            write_reports(self.report, self.output)
        self.assertFalse((self.output / "findings.csv").exists())

    def test_report_is_deterministic_when_input_order_changes(self):
        assets, schedules, exceptions = load_inputs(ROOT / "data")
        reversed_report = check_coverage(
            list(reversed(assets)),
            list(reversed(schedules)),
            list(reversed(exceptions)),
            as_of=AS_OF,
        )
        self.assertEqual(render_summary(self.report), render_summary(reversed_report))

    def test_cli_findings_exit_1_and_overwrite_exit_2(self):
        args = ["--as-of", "2026-10-13", "--output-dir", str(self.output)]
        with redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(main(args), 1)
        self.assertIn("확인 필요 4대", stream.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 2)
            self.assertEqual(main(args + ["--overwrite"]), 1)

    def test_cli_normal_exit_0(self):
        data = Path(self.temp.name) / "data"
        data.mkdir()
        source = (ROOT / "data" / "assets.csv").read_text(encoding="utf-8").splitlines()
        (data / "assets.csv").write_text("\n".join(source[:2]) + "\n", encoding="utf-8")
        for name in ("schedules.csv", "exceptions.csv"):
            source = (ROOT / "data" / name).read_text(encoding="utf-8").splitlines()
            keep = source[:2] if name == "schedules.csv" else source[:1]
            (data / name).write_text("\n".join(keep) + "\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "--as-of",
                        "2026-10-13",
                        "--data-dir",
                        str(data),
                        "--output-dir",
                        str(self.output),
                    ]
                ),
                0,
            )

    def test_cli_input_error_leaves_reports_uncreated(self):
        with redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(
                main(
                    [
                        "--data-dir",
                        str(Path(self.temp.name) / "SECRET_SENTINEL"),
                        "--output-dir",
                        str(self.output),
                    ]
                ),
                2,
            )
        self.assertFalse(self.output.exists())
        self.assertNotIn("SECRET_SENTINEL", stream.getvalue())

    def test_cli_invalid_date_and_days_exit_2(self):
        for args in (
            ["--as-of", "2026-02-30"],
            ["--stale-after-days", "0"],
            ["--stale-after-days", "abc"],
        ):
            with (
                self.subTest(args=args),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                main(args)
            self.assertEqual(caught.exception.code, 2)

    def test_cli_invalid_required_tags_does_not_write_reports(self):
        with redirect_stdout(io.StringIO()) as stream:
            code = main(
                [
                    "--as-of",
                    "2026-10-13",
                    "--required-tags",
                    "=SECRET_SENTINEL",
                    "--output-dir",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 2)
        self.assertFalse(self.output.exists())
        self.assertNotIn("SECRET_SENTINEL", stream.getvalue())

    def test_cli_save_error_uses_sanitized_message(self):
        with patch("main.write_reports", side_effect=OSError("SECRET_SENTINEL")):
            with redirect_stdout(io.StringIO()) as stream:
                code = main(["--as-of", "2026-10-13", "--output-dir", str(self.output)])
        self.assertEqual(code, 2)
        self.assertIn("일부 파일이 작성되었을 수 있습니다", stream.getvalue())
        self.assertNotIn("SECRET_SENTINEL", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
