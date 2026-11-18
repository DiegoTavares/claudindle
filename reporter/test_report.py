import json
import unittest

import report

T0 = "2026-11-18T18:05:00Z"
EPOCH0 = 1795025100  # T0 as epoch seconds


def entry(**kw) -> str:
    base = {"timestamp": T0, "sessionId": "s1", "cwd": "/home/d/dev/thock"}
    base.update(kw)
    return json.dumps(base)


def prompt(uuid: str, text="hello", **kw) -> str:
    return entry(type="user", uuid=uuid, message={"role": "user", "content": text}, **kw)


def reply(uuid: str, mid: str, content=(), model="claude-opus-5-5", **kw) -> str:
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 100,
             "cache_read_input_tokens": 9999}
    return entry(type="assistant", uuid=uuid,
                 message={"id": mid, "model": model, "usage": usage, "content": list(content)}, **kw)


class ProjectNameTest(unittest.TestCase):
    def test_worktrees_fold_into_parent_repo(self):
        self.assertEqual(report.project_name("/home/d/dev/thock/.claude/worktrees/v41", "x"), "thock")

    def test_missing_cwd_uses_fallback(self):
        self.assertEqual(report.project_name(None, "x"), "x")


class IsPromptTest(unittest.TestCase):
    def test_typed_text_is_a_prompt(self):
        self.assertTrue(report.is_prompt(json.loads(prompt("u1"))))

    def test_commands_meta_and_subagents_are_not(self):
        self.assertFalse(report.is_prompt(json.loads(prompt("u1", "<command-name>/clear</command-name>"))))
        self.assertFalse(report.is_prompt(json.loads(prompt("u1", isMeta=True))))
        self.assertFalse(report.is_prompt(json.loads(prompt("u1", isSidechain=True))))

    def test_tool_results_are_not_prompts_but_text_with_images_is(self):
        tool = [{"type": "tool_result", "content": "ok"}]
        image = [{"type": "image"}, {"type": "text", "text": "look"}]
        self.assertFalse(report.is_prompt(json.loads(prompt("u1", tool))))
        self.assertTrue(report.is_prompt(json.loads(prompt("u1", image))))


class SummarizeTest(unittest.TestCase):
    def test_counts_prompts_tokens_and_tools_once(self):
        tool = {"type": "tool_use", "name": "Bash"}
        lines = [
            prompt("u1"),
            prompt("u1"),  # copied into a resumed session's file
            reply("a1", "m1", [{"type": "text"}]),
            reply("a2", "m1", [tool]),  # same response, next content block
            "not json",
        ]
        out = report.summarize(lines, since=0)
        hour = EPOCH0 // 3600 * 3600
        self.assertEqual(out["prompts"], [[EPOCH0, "thock", "s1"]])
        self.assertEqual(out["tokens"], [[hour, "thock", "claude-opus-5-5", 115]])
        self.assertEqual(out["tools"], [[hour, "Bash", 1]])

    def test_skips_old_entries_and_synthetic_models(self):
        lines = [prompt("u1"), reply("a1", "m1", model="<synthetic>")]
        self.assertEqual(report.summarize(lines, since=EPOCH0 + 1)["prompts"], [])
        self.assertEqual(report.summarize(lines, since=0)["tokens"], [])


if __name__ == "__main__":
    unittest.main()
