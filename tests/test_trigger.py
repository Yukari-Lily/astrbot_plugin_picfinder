"""搜图触发判定测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.trigger import is_bare_keyword, keyword_hit, should_search  # noqa: E402

KEYWORDS = ("识图", "搜图", "查图", "找图", "识图喵")


class TestKeywordHit(unittest.TestCase):
    def test_anywhere_in_message(self):
        self.assertTrue(keyword_hit("帮我识图一下", KEYWORDS))
        self.assertTrue(keyword_hit("识图", KEYWORDS))
        self.assertTrue(keyword_hit("这图能搜图吗", KEYWORDS))
        self.assertTrue(keyword_hit("识图喵！", KEYWORDS))

    def test_no_keyword(self):
        self.assertFalse(keyword_hit("llr真的有这么大吗", KEYWORDS))
        self.assertFalse(keyword_hit("这就是llr的丑态", KEYWORDS))
        self.assertFalse(keyword_hit("", KEYWORDS))


class TestBareKeyword(unittest.TestCase):
    def test_only_keyword(self):
        self.assertTrue(is_bare_keyword("识图", KEYWORDS))
        self.assertTrue(is_bare_keyword("识图喵", KEYWORDS))
        self.assertTrue(is_bare_keyword(" /识图 ", KEYWORDS))
        self.assertTrue(is_bare_keyword("识图。", KEYWORDS))

    def test_keyword_inside_sentence(self):
        self.assertFalse(is_bare_keyword("我不会识图", KEYWORDS))
        self.assertFalse(is_bare_keyword("帮我识图一下", KEYWORDS))
        self.assertFalse(is_bare_keyword("", KEYWORDS))


class TestShouldSearch(unittest.TestCase):
    def test_keyword_with_picture(self):
        self.assertTrue(should_search(keyword=True, has_image=True))

    def test_keyword_without_picture_goes_to_batch(self):
        self.assertFalse(should_search(keyword=True, has_image=False))

    def test_picture_without_keyword_is_plain_chat(self):
        # 唤醒词 / 单纯 @ 机器人 / 引用图片闲聊 都不该抢
        self.assertFalse(should_search(keyword=False, has_image=True))
        self.assertFalse(should_search(keyword=False, has_image=False))


if __name__ == "__main__":
    unittest.main()
