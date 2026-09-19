import unittest

from scripts.approval_domain import (
    ApprovalValidationError,
    extract_design_command_from_gmail,
    extract_gate_a_command_from_gmail,
    normalize_design_command,
    normalize_gate_command,
)
from scripts.check_design_selection import extract_command as legacy_design_extract
from scripts.check_story_approval import (
    VALID_APPROVAL_RE,
    normalize_reply as legacy_gate_normalize,
)


def legacy_gate_result(body):
    normalized = legacy_gate_normalize(body)
    if VALID_APPROVAL_RE.fullmatch(normalized):
        return normalize_gate_command(normalized)
    return None


def new_gate_result(body):
    try:
        return normalize_gate_command(extract_gate_a_command_from_gmail(body))
    except ApprovalValidationError:
        return None


def legacy_design_result(body):
    extracted = legacy_design_extract(body)
    if extracted is None:
        return None
    command_type, image_number, title_number, _ = extracted
    if command_type == "NEXT_3":
        return "NEXT_3"
    return f"SELECT_DESIGN:{image_number}:{title_number}"


def new_design_result(body):
    try:
        return normalize_design_command(extract_design_command_from_gmail(body))
    except ApprovalValidationError:
        return None


def legacy_gate_wire(body):
    normalized = legacy_gate_normalize(body)
    return normalized if VALID_APPROVAL_RE.fullmatch(normalized) else None


def new_gate_wire(body):
    try:
        return extract_gate_a_command_from_gmail(body)
    except ApprovalValidationError:
        return None


def legacy_design_wire(body):
    extracted = legacy_design_extract(body)
    return extracted[3] if extracted is not None else None


def new_design_wire(body):
    try:
        return extract_design_command_from_gmail(body)
    except ApprovalValidationError:
        return None


class GateAGmailDifferentialTests(unittest.TestCase):
    CASES = (
        ("simple", "3"),
        ("whitespace", " 3 "),
        ("trailing_newline", "3\n"),
        ("quoted_reply", "3\n> quoted 5"),
        ("multiple_quoted_lines", "3\n> quoted 4\n> quoted 5"),
        ("on_wrote", "3\nOn Tuesday someone wrote:"),
        ("trailing_thanks", "3\nThanks!"),
        ("iphone_signature", "3\nSent from my iPhone"),
        ("duplicate_same", "3\n3"),
        ("conflicting", "3\n4"),
        ("invalid", "OK"),
        ("full_width_digit", "３"),
    )

    def test_matches_legacy_for_gate_a_matrix(self):
        for name, body in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(new_gate_result(body), legacy_gate_result(body))

    def test_gate_a_legacy_acceptance_and_rejection_are_preserved(self):
        self.assertEqual(new_gate_result("3\n> quoted 5"), "SELECT_STORY:3")
        for body in ("3\nThanks!", "3\nSent from my iPhone", "3\n3", "3\n4"):
            with self.subTest(body=body):
                self.assertIsNone(new_gate_result(body))


class DesignGmailDifferentialTests(unittest.TestCase):
    CASES = (
        ("simple_selection", "1 3"),
        ("whitespace", " 1 3 "),
        ("quoted_reply", "1 3\n> quoted original"),
        ("trailing_thanks", "1 3\nThanks!"),
        ("iphone_signature", "1 3\nSent from my iPhone"),
        ("duplicate_selection", "1 3\n1 3"),
        ("conflicting_selections", "1 3\n2 2"),
        ("next_quoted", "NEXT 3\n> quoted original"),
        ("next_trailing_text", "NEXT 3\nThanks!"),
        ("duplicate_next", "NEXT 3\nNEXT 3"),
        ("next_selection_conflict", "NEXT 3\n1 3"),
        ("full_width", "１　３"),
        ("invalid", "Thanks!"),
    )

    def test_matches_legacy_for_design_matrix(self):
        for name, body in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(new_design_result(body), legacy_design_result(body))

    def test_design_quoted_and_trailing_text_are_accepted(self):
        for body in (
            "1 3\n> quoted original",
            "1 3\nThanks!",
            "1 3\nSent from my iPhone",
        ):
            with self.subTest(body=body):
                self.assertEqual(new_design_result(body), "SELECT_DESIGN:1:3")

    def test_design_duplicate_commands_are_accepted(self):
        self.assertEqual(new_design_result("1 3\n1 3"), "SELECT_DESIGN:1:3")
        self.assertEqual(new_design_result("NEXT 3\nNEXT 3"), "NEXT_3")

    def test_design_conflicting_commands_are_rejected(self):
        self.assertIsNone(new_design_result("1 3\n2 2"))
        self.assertIsNone(new_design_result("NEXT 3\n1 3"))


class StructuredEventStrictnessTests(unittest.TestCase):
    def test_gate_a_structured_command_does_not_parse_gmail_body(self):
        with self.assertRaises(ApprovalValidationError):
            normalize_gate_command("3\n> quoted 5")

    def test_design_structured_command_does_not_parse_gmail_body(self):
        for body in ("1 3\nThanks!", "1 3\n1 3", "NEXT 3\n> quoted"):
            with self.subTest(body=body), self.assertRaises(ApprovalValidationError):
                normalize_design_command(body)


class JapaneseQuoteBoundaryDifferentialTests(unittest.TestCase):
    GATE_CASES = (
        (
            "original_message_after_fresh_command",
            "3\n-----元のメッセージ-----\n5",
            "3",
        ),
        (
            "japanese_date_after_fresh_command",
            "3\n2026年9月19日 Alice <a@b.com>:\n5",
            "3",
        ),
        (
            "no_fresh_command_before_original_message",
            "fresh text contains no command\n-----元のメッセージ-----\n3",
            None,
        ),
    )
    DESIGN_CASES = (
        (
            "original_message_after_next",
            "NEXT 3\n-----元のメッセージ-----\n1 3",
            "NEXT 3",
        ),
        (
            "quoted_source_after_next",
            "NEXT 3\n----- 引用元メッセージ -----\n1 3",
            "NEXT 3",
        ),
        (
            "no_fresh_command_before_original_message",
            "fresh text contains no command\n-----元のメッセージ-----\n1 3",
            None,
        ),
        (
            "no_fresh_command_before_quoted_source",
            "fresh text contains no command\n----- 引用元メッセージ -----\nNEXT 3",
            None,
        ),
        (
            "quoted_only",
            "-----元のメッセージ-----\n1 3",
            None,
        ),
    )

    def test_gate_a_japanese_boundaries_match_legacy_at_wire_level(self):
        for name, body, expected in self.GATE_CASES:
            with self.subTest(name=name):
                legacy = legacy_gate_wire(body)
                new = new_gate_wire(body)
                self.assertEqual(legacy, expected)
                self.assertEqual(new, expected)
                self.assertEqual(new, legacy)

    def test_design_japanese_boundaries_match_legacy_at_wire_level(self):
        for name, body, expected in self.DESIGN_CASES:
            with self.subTest(name=name):
                legacy = legacy_design_wire(body)
                new = new_design_wire(body)
                self.assertEqual(legacy, expected)
                self.assertEqual(new, expected)
                self.assertEqual(new, legacy)

    def test_quoted_valid_command_without_fresh_command_is_rejected(self):
        attacks = (
            "やっぱり考えます\n-----元のメッセージ-----\n1 3",
            "考え直します\n----- 引用元メッセージ -----\nNEXT 3",
        )
        for body in attacks:
            with self.subTest(body=body):
                self.assertIsNone(legacy_design_wire(body))
                self.assertIsNone(new_design_wire(body))


if __name__ == "__main__":
    unittest.main()
