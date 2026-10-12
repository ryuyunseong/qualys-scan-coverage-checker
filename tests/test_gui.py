import io
import tempfile
import tkinter as tk
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import patch

import gui
from checker import InputError, check_coverage, load_inputs, render_summary

ROOT = Path(__file__).resolve().parents[1]


class GuiServiceTests(unittest.TestCase):
    def test_gui_and_cli_use_identical_sample_results(self):
        actual = gui.review_csv(
            str(ROOT / "data"), "2026-10-13", "30", "region,env,role"
        )
        expected = check_coverage(*load_inputs(ROOT / "data"), as_of=date(2026, 10, 13))
        self.assertEqual(actual, expected)

    def test_invalid_date_message_does_not_include_raw_value(self):
        for value in ("2026-02-30", "20261013", "SECRET_SENTINEL"):
            with self.subTest(value=value), self.assertRaises(InputError) as caught:
                gui.review_csv(str(ROOT / "data"), value, "30", "region,env,role")
            self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_invalid_days_are_rejected(self):
        for value in ("", "0", "-1", "1.5", "SECRET_SENTINEL"):
            with self.subTest(value=value), self.assertRaises(InputError) as caught:
                gui.review_csv(
                    str(ROOT / "data"), "2026-10-13", value, "region,env,role"
                )
            self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_invalid_tag_policy_is_rejected(self):
        for value in ("", "env,env", "=SECRET_SENTINEL"):
            with self.subTest(value=value), self.assertRaises(InputError) as caught:
                gui.review_csv(str(ROOT / "data"), "2026-10-13", "30", value)
            self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_missing_or_empty_input_folder_is_rejected(self):
        for folder in ("", str(ROOT / "SECRET_SENTINEL")):
            with self.subTest(folder=folder), self.assertRaises(InputError) as caught:
                gui.review_csv(folder, "2026-10-13", "30", "region,env,role")
            self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_control_characters_in_folder_path_are_rejected(self):
        for folder in ("SECRET_SENTINEL\x00", "SECRET_SENTINEL\n"):
            with self.subTest(folder=folder), self.assertRaises(InputError) as caught:
                gui.review_csv(folder, "2026-10-13", "30", "region,env,role")
            self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_form_spaces_are_trimmed(self):
        report = gui.review_csv(
            f" {ROOT / 'data'} ", " 2026-10-13 ", " 30 ", " region, env, role "
        )
        self.assertEqual(report.count_status("확인 필요"), 4)

    def test_save_keeps_existing_reports_without_explicit_overwrite(self):
        report = gui.review_csv(
            str(ROOT / "data"), "2026-10-13", "30", "region,env,role"
        )
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            gui.save_review(report, ROOT / "data", output, overwrite=False)
            original = (output / "summary.md").read_bytes()
            with self.assertRaises(InputError):
                gui.save_review(report, ROOT / "data", output, overwrite=False)
            self.assertEqual((output / "summary.md").read_bytes(), original)
            gui.save_review(report, ROOT / "data", output, overwrite=True)
            self.assertEqual((output / "summary.md").read_bytes(), original)

    def test_unavailable_desktop_has_readable_startup_error(self):
        with patch("gui.tk.Tk", side_effect=tk.TclError("SECRET_SENTINEL")):
            with redirect_stderr(io.StringIO()) as stream:
                self.assertEqual(gui.main(), 2)
        self.assertIn("GUI를 시작할 수 없습니다", stream.getvalue())
        self.assertNotIn("SECRET_SENTINEL", stream.getvalue())


class GuiWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.window = tk.Tk()
        except tk.TclError:
            self.skipTest("Tkinter 화면 세션이 없어 GUI 창 검증을 실행할 수 없습니다.")
        self.addCleanup(self._close_window)
        self.window.withdraw()
        self.app = gui.CoverageApp(self.window)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def _close_window(self):
        self.window.update()
        self.window.destroy()

    def test_save_is_disabled_before_review(self):
        self.assertIsNone(self.app.report)
        self.assertTrue(self.app.save_button.instate(["disabled"]))
        self.assertEqual(self.app.tree.get_children(), ())

    def test_example_displays_all_assets_and_counts(self):
        self.app.run_example()
        self.assertEqual(
            [value.get() for value in self.app.totals], ["12", "8", "8", "0", "4"]
        )
        self.assertEqual(len(self.app.tree.get_children()), 12)
        self.assertFalse(self.app.save_button.instate(["disabled"]))

    def test_filter_changes_view_without_changing_report(self):
        self.app.run_example()
        snapshot = self.app.report
        for status, count in (
            ("확인 필요", 4),
            ("정상", 8),
            ("예외 적용", 0),
            ("전체", 12),
        ):
            with self.subTest(status=status):
                self.app.filter.set(status)
                self.assertEqual(len(self.app.tree.get_children()), count)
                self.assertIs(self.app.report, snapshot)

    def test_selected_asset_shows_findings_and_exception_date(self):
        self.app.run_example()
        self.app.tree.selection_set("A005")
        self.app._show_selected()
        text = self.app.details.get("1.0", "end")
        self.assertIn("US-APP-05", text)
        self.assertIn("예외 만료: 2026-10-12", text)
        self.assertIn("EX001", text)
        self.assertEqual(str(self.app.details.cget("state")), "disabled")

    def test_each_input_change_invalidates_previous_results(self):
        for variable, value in (
            (self.app.data_dir, str(ROOT / "other")),
            (self.app.as_of, "2026-10-14"),
            (self.app.days, "14"),
            (self.app.required_tags, "region"),
        ):
            with self.subTest(value=value):
                self.app.run_example()
                variable.set(value)
                self.assertIsNone(self.app.report)
                self.assertEqual(self.app.tree.get_children(), ())
                self.assertTrue(self.app.save_button.instate(["disabled"]))

    def test_failed_review_does_not_keep_old_results(self):
        self.app.run_example()
        self.app.as_of.set("SECRET_SENTINEL")
        with patch("gui.messagebox.showerror") as dialog:
            self.app.run_review()
        dialog.assert_called_once()
        self.assertNotIn("SECRET_SENTINEL", dialog.call_args.args[1])
        self.assertIsNone(self.app.report)
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertTrue(self.app.save_button.instate(["disabled"]))
        self.assertFalse(self.app.run_button.instate(["disabled"]))

    def test_cancel_input_picker_preserves_review(self):
        self.app.run_example()
        snapshot = self.app.report
        with patch("gui.filedialog.askdirectory", return_value=""):
            self.app._choose_input()
        self.assertIs(self.app.report, snapshot)

    def test_saving_without_review_does_not_open_picker(self):
        with (
            patch("gui.messagebox.showinfo") as message,
            patch("gui.filedialog.askdirectory") as picker,
        ):
            self.app.save_results()
        message.assert_called_once()
        picker.assert_not_called()

    def test_cancel_save_does_not_write(self):
        self.app.run_example()
        with (
            patch("gui.filedialog.askdirectory", return_value=""),
            patch("gui.save_review") as save,
        ):
            self.app.save_results()
        save.assert_not_called()

    def test_filtered_view_still_saves_full_report(self):
        self.app.run_example()
        self.app.filter.set("확인 필요")
        with (
            patch("gui.filedialog.askdirectory", return_value=str(self.output)),
            patch("gui.messagebox.showinfo"),
        ):
            self.app.save_results()
        summary = (self.output / "summary.md").read_text(encoding="utf-8")
        self.assertEqual(summary, render_summary(self.app.report))
        self.assertIn("KR-WEB-01", summary)
        self.assertIn("US-APP-05", summary)

    def test_overwrite_decline_preserves_file(self):
        self.app.run_example()
        (self.output / "summary.md").write_text("keep", encoding="utf-8")
        with (
            patch("gui.filedialog.askdirectory", return_value=str(self.output)),
            patch("gui.messagebox.askyesno", return_value=False) as confirm,
            patch("gui.save_review") as save,
        ):
            self.app.save_results()
        confirm.assert_called_once()
        save.assert_not_called()
        self.assertEqual(
            (self.output / "summary.md").read_text(encoding="utf-8"), "keep"
        )

    def test_overwrite_accept_updates_report(self):
        self.app.run_example()
        (self.output / "summary.md").write_text("old", encoding="utf-8")
        with (
            patch("gui.filedialog.askdirectory", return_value=str(self.output)),
            patch("gui.messagebox.askyesno", return_value=True),
            patch("gui.messagebox.showinfo"),
        ):
            self.app.save_results()
        self.assertEqual(
            (self.output / "summary.md").read_text(encoding="utf-8"),
            render_summary(self.app.report),
        )

    def test_save_failure_does_not_expose_os_error(self):
        self.app.run_example()
        with (
            patch("gui.filedialog.askdirectory", return_value=str(self.output)),
            patch("gui.save_review", side_effect=OSError("SECRET_SENTINEL")),
            patch("gui.messagebox.showerror") as message,
        ):
            self.app.save_results()
        self.assertNotIn("SECRET_SENTINEL", message.call_args.args[1])
        self.assertIn("일부 파일이 작성되었을 수 있습니다", message.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
