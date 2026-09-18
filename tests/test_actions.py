"""Chord parsing and binding descriptions.

Nothing here posts an event: parse_chord and describe are pure, which is the
whole reason they are worth pinning down.
"""
import unittest

import Quartz

import actions
from actions import KEY_CODES, MODIFIER_FLAGS, ActionError, describe, parse_chord

CMD = Quartz.kCGEventFlagMaskCommand
SHIFT = Quartz.kCGEventFlagMaskShift
ALT = Quartz.kCGEventFlagMaskAlternate
CTRL = Quartz.kCGEventFlagMaskControl
FN = Quartz.kCGEventFlagMaskSecondaryFn


class ParseChordTests(unittest.TestCase):

    def test_a_bare_key_has_no_modifiers(self):
        self.assertEqual(parse_chord("a"), (0, 0))

    def test_a_modified_key(self):
        self.assertEqual(parse_chord("cmd+shift+4"), (KEY_CODES["4"], CMD | SHIFT))

    def test_every_key_name_parses_to_its_code(self):
        for name, code in KEY_CODES.items():
            with self.subTest(key=name):
                self.assertEqual(parse_chord(name), (code, 0))

    def test_every_modifier_name_parses_to_its_flag(self):
        for name, flag in MODIFIER_FLAGS.items():
            with self.subTest(modifier=name):
                self.assertEqual(parse_chord(f"{name}+a"), (0, flag))

    def test_modifier_aliases_agree(self):
        self.assertEqual(parse_chord("cmd+a"), parse_chord("command+a"))
        self.assertEqual(parse_chord("alt+a"), parse_chord("option+a"))
        self.assertEqual(parse_chord("alt+a"), parse_chord("opt+a"))
        self.assertEqual(parse_chord("ctrl+a"), parse_chord("control+a"))

    def test_key_aliases_agree(self):
        self.assertEqual(parse_chord("return"), parse_chord("enter"))
        self.assertEqual(parse_chord("delete"), parse_chord("backspace"))
        self.assertEqual(parse_chord("escape"), parse_chord("esc"))

    def test_case_is_ignored(self):
        self.assertEqual(parse_chord("CMD+Shift+A"), (KEY_CODES["a"], CMD | SHIFT))

    def test_spaces_around_the_parts_are_ignored(self):
        self.assertEqual(parse_chord(" cmd + shift + a "),
                         (KEY_CODES["a"], CMD | SHIFT))

    def test_modifier_order_does_not_matter(self):
        self.assertEqual(parse_chord("shift+cmd+a"), parse_chord("cmd+shift+a"))

    def test_four_modifiers_at_once(self):
        keycode, flags = parse_chord("ctrl+alt+shift+cmd+k")
        self.assertEqual(keycode, KEY_CODES["k"])
        self.assertEqual(flags, CTRL | ALT | SHIFT | CMD)

    def test_a_repeated_modifier_is_harmless(self):
        self.assertEqual(parse_chord("cmd+cmd+a"), (KEY_CODES["a"], CMD))

    def test_the_fn_modifier(self):
        self.assertEqual(parse_chord("fn+f1"), (KEY_CODES["f1"], FN))

    def test_punctuation_keys(self):
        self.assertEqual(parse_chord("cmd+-"), (KEY_CODES["-"], CMD))
        self.assertEqual(parse_chord("cmd+shift+="), (KEY_CODES["="], CMD | SHIFT))

    def test_an_empty_chord_is_refused(self):
        for text in ("", "   ", "+", "++", " + "):
            with self.subTest(text=text):
                with self.assertRaises(ActionError) as caught:
                    parse_chord(text)
                self.assertIn("Empty", str(caught.exception))

    def test_an_unknown_modifier_is_refused(self):
        with self.assertRaises(ActionError) as caught:
            parse_chord("meta+a")
        self.assertIn("meta", str(caught.exception))
        self.assertIn("modifier", str(caught.exception))

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(ActionError) as caught:
            parse_chord("cmd+nope")
        self.assertIn("nope", str(caught.exception))
        self.assertIn("cmd+nope", str(caught.exception))

    def test_a_trailing_plus_leaves_a_modifier_where_the_key_should_be(self):
        with self.assertRaises(ActionError) as caught:
            parse_chord("cmd+")
        self.assertIn("cmd", str(caught.exception))

    def test_a_modifier_alone_is_not_a_key(self):
        for text in ("cmd", "shift", "ctrl+shift"):
            with self.subTest(text=text):
                with self.assertRaises(ActionError):
                    parse_chord(text)

    def test_a_key_in_the_modifier_position_is_refused(self):
        with self.assertRaises(ActionError):
            parse_chord("a+b")

    def test_unknown_keys_and_modifiers_raise_the_same_family(self):
        # Callers catch ActionError; nothing may escape as a bare ValueError.
        for text in ("meta+a", "cmd+nope", "", "cmd+"):
            with self.subTest(text=text):
                with self.assertRaises(actions.ActionError):
                    parse_chord(text)


class DescribeTests(unittest.TestCase):

    def test_key_binding(self):
        self.assertEqual(describe({"type": "keys", "keys": "cmd+shift+4"}),
                         "cmd+shift+4")

    def test_text_binding(self):
        self.assertEqual(describe({"type": "text", "text": "hello"}),
                         'type "hello"')

    def test_text_is_truncated_with_an_ellipsis(self):
        self.assertEqual(describe({"type": "text", "text": "x" * 18}),
                         'type "%s"' % ("x" * 18))
        self.assertEqual(describe({"type": "text", "text": "x" * 19}),
                         'type "%s..."' % ("x" * 18))

    def test_shell_binding(self):
        self.assertEqual(describe({"type": "shell", "command": "say hello"}),
                         "$ say hello")

    def test_shell_is_truncated_with_an_ellipsis(self):
        self.assertEqual(describe({"type": "shell", "command": "y" * 22}),
                         "$ %s" % ("y" * 22))
        self.assertEqual(describe({"type": "shell", "command": "y" * 23}),
                         "$ %s..." % ("y" * 22))

    def test_app_binding(self):
        self.assertEqual(describe({"type": "app", "name": "Safari"}),
                         "open Safari")

    def test_macro_binding(self):
        self.assertEqual(describe({"type": "macro", "name": "G1-m1"}),
                         "macro G1-m1")

    def test_nothing_bound(self):
        self.assertEqual(describe(None), "-")
        self.assertEqual(describe({}), "-")

    def test_a_missing_field_does_not_crash(self):
        self.assertEqual(describe({"type": "keys"}), "?")
        self.assertEqual(describe({"type": "app"}), "open ?")
        self.assertEqual(describe({"type": "macro"}), "macro ?")
        self.assertEqual(describe({"type": "text"}), 'type ""')
        self.assertEqual(describe({"type": "shell"}), "$ ")

    def test_an_unknown_type_is_shown_as_itself(self):
        self.assertEqual(describe({"type": "teleport"}), "teleport")

    def test_a_binding_with_no_type_does_not_crash(self):
        result = describe({"name": "Safari"})
        self.assertIsInstance(result, str)
        self.assertTrue(result)

    def test_every_well_formed_type_survives_a_missing_field(self):
        for kind in ("keys", "text", "shell", "app", "macro", "nonsense"):
            with self.subTest(kind=kind):
                result = describe({"type": kind})
                self.assertIsInstance(result, str)
                self.assertTrue(result)

    def test_a_non_string_name_is_survivable(self):
        self.assertEqual(describe({"type": "app", "name": 7}), "open 7")
        self.assertEqual(describe({"type": "macro", "name": None}), "macro None")

    @unittest.expectedFailure
    def test_a_field_of_the_wrong_type_does_not_crash(self):
        # BUG: describe() trusts the field types a config file happens to
        # carry. A null or numeric "keys"/"text"/"command" - easy to leave
        # behind in a hand edit, and what an unfinished GUI binding looks like
        # - either returns None instead of a string ("keys") or raises
        # TypeError on the slice ("text", "shell"). Reported, not fixed.
        for binding in ({"type": "keys", "keys": None},
                        {"type": "text", "text": None},
                        {"type": "text", "text": 5},
                        {"type": "shell", "command": 5}):
            with self.subTest(binding=binding):
                self.assertIsInstance(describe(binding), str)

    def test_the_default_bindings_all_describe(self):
        import config
        for key, binding in config.DEFAULTS["bindings"].items():
            with self.subTest(key=key):
                self.assertTrue(describe(binding))

    @unittest.expectedFailure
    def test_a_binding_that_is_not_a_dict_does_not_crash(self):
        # BUG: describe() calls binding.get() on whatever it is handed, so a
        # hand-edited config with "G1": "cmd+c" raises AttributeError in the
        # menus and listings rather than showing anything. Reported, not fixed.
        for binding in ("cmd+c", ["cmd+c"], 7):
            with self.subTest(binding=binding):
                self.assertIsInstance(describe(binding), str)


if __name__ == "__main__":
    unittest.main()
