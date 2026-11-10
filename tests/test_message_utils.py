"""消息与图片解析单元测试。"""

import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from message_utils import _first_image_from_cq_string


class TestMessageUtils(unittest.TestCase):
    """测试 CQ 码解析与图片地址提取"""

    def test_cq_image_url_extraction(self):
        cq_text = "识图[CQ:image,file=1C443957.png,url=https://multimedia.nt.qq.com.cn/download?appid=1407&amp;fileid=abc12345]"
        url = _first_image_from_cq_string(cq_text)
        self.assertIsNotNone(url)
        self.assertEqual(url, "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc12345")

    def test_cq_image_suffix(self):
        cq_text = "[CQ:image,file=xyz.jpg,url=https://gchat.qpic.cn/gchatpic_new/0/0-0-ABC/0] 识图"
        url = _first_image_from_cq_string(cq_text)
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("https://gchat.qpic.cn/"))


if __name__ == "__main__":
    unittest.main()
