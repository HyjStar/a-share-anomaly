"""静态页面渲染测试，重点验证 JSON 注入安全。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from anomaly.web import render_html

ROOT = Path(__file__).resolve().parents[1]


class WebRenderTest(unittest.TestCase):
    def test_embedded_json_is_escaped_and_parseable(self):
        payload = {
            "meta": {"title": "<测试标题>"},
            "evil": "</script><script>alert(1)</script>",
            "normal": "A股异动",
        }
        html = render_html(payload, ROOT / "web" / "template.html")
        self.assertNotIn("</script><script>alert(1)</script>", html)
        self.assertIn("\\u003c", html)
        marker_start = '<script id="app-data" type="application/json">'
        start = html.index(marker_start) + len(marker_start)
        end = html.index("</script>", start)
        embedded = html[start:end]
        decoded = json.loads(embedded)
        self.assertEqual(decoded["evil"], payload["evil"])
        self.assertEqual(decoded["normal"], "A股异动")
        self.assertIn("&lt;测试标题&gt;", html)


if __name__ == "__main__":
    unittest.main()
