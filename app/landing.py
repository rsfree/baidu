"""根路径落地页（`GET /`）—— 给人看的入口说明。

为什么要有它：本服务是**纯 API**，`/` 默认是裸 JSON 404；浏览器打开观感等于"打不开"
（2026-09-24 的真实工单）。内容**从注册表派生**（版本/能力数/可用数），渲染成静态 HTML。
"""

from __future__ import annotations

from app import __version__
from app.config import Settings
from app.models import CAPABILITIES, available

_STYLE = """
  :root { color-scheme: light dark; }
  body { margin:0; font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif;
         background:#f6f7f9; color:#1a1a1a; }
  .wrap { max-width:760px; margin:0 auto; padding:48px 22px 64px; }
  h1 { font-size:26px; margin:0 0 6px; letter-spacing:.2px; }
  .sub { color:#5b6470; margin:0 0 26px; }
  .card { background:#fff; border:1px solid #e6e8eb; border-radius:12px; padding:20px 22px; margin:0 0 18px; }
  h2 { font-size:15px; margin:0 0 10px; color:#0b57d0; letter-spacing:.3px; }
  code, pre { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  pre { background:#0f172a; color:#e6edf3; padding:14px 16px; border-radius:8px; overflow:auto; font-size:12.5px; }
  a { color:#0b57d0; text-decoration:none; } a:hover { text-decoration:underline; }
  ul { margin:6px 0 0; padding-left:20px; } li { margin:3px 0; }
  .tag { display:inline-block; background:#eef2ff; color:#34418a; border-radius:99px; padding:1px 9px; font-size:12px; margin-right:6px; }
  footer { color:#8a929c; font-size:12.5px; margin-top:26px; }
  @media (prefers-color-scheme: dark) {
    body { background:#0b0f14; color:#e6edf3; }
    .card { background:#111823; border-color:#1e2836; }
    h2 { color:#7cb0ff; } a { color:#7cb0ff; } .sub { color:#98a2b3; }
    .tag { background:#1c2433; color:#a9bcf0; } footer { color:#6b7684; }
  }
"""


def render(settings: Settings) -> str:
    """渲染落地页（纯函数：同一份 settings 永远得到同一份 HTML）。"""
    legacy = settings.LEGACY != "off"
    usable = len(available(allow_unverified=bool(settings.ALLOW_UNVERIFIED), legacy=legacy))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>baidu-service · 文心助手图片编辑 API</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
  <h1>baidu-service</h1>
  <p class="sub">文心助手（<code>wenxin.baidu.com</code> / <code>chat.baidu.com</code>）图片编辑的 OpenAI 风格同步出口 ——
     <strong>一条 POST、图进图出</strong>。<br>本页只是入口说明；服务本身是 <strong>API</strong>，没有网页 UI。</p>

  <div class="card">
    <h2>怎么调</h2>
    <pre>curl -sS &lt;本机地址&gt;/v1/images/generations \\
  -H "Authorization: Bearer $BAIDU_API_KEY" \\
  -H 'Content-Type: application/json' \\
  -d '{{"model":"wenxin:clarity","image":"https://example.com/a.jpg"}}'</pre>
    <ul>
      <li><code>image</code> 支持 <b>http(s) URL</b> / <b>data URI</b> / 裸 base64</li>
      <li>换风格需 <code>style</code>（17 项，如 <code>宫崎骏风</code>）；消除/局部替换/背景替换需 <code>mask</code>（黑底白框，白=要处理）</li>
      <li>想零成本试跑：请求体加 <code>"dry_run": true</code>（只回将发出的上游请求计划）</li>
    </ul>
  </div>

  <div class="card">
    <h2>探路（均免鉴权）</h2>
    <ul>
      <li><a href="/llms.txt">/llms.txt</a> —— 给 LLM/Agent 的说明书（能力表 / 参数 / 风格表 / 错误码）</li>
      <li><a href="/v1/models">/v1/models</a> —— 本部署可调用的模型</li>
      <li><a href="/healthz">/healthz</a> · <a href="/readyz">/readyz</a> —— 存活 / 就绪</li>
    </ul>
  </div>

  <div class="card">
    <h2>状态</h2>
    <ul>
      <li><span class="tag">版本 {__version__}</span><span class="tag">{len(CAPABILITIES)} 项能力</span>
          <span class="tag">本部署可用 {usable}</span><span class="tag">鉴权 fail-closed</span></li>
      <li>上游通路：主链（<code>chat.baidu.com</code>）+ 老接口兜底（<code>BAIDU_LEGACY={settings.LEGACY}</code>）</li>
      <li>验证用例 / 契约：仓库的 <code>tests/</code> · <code>docs/INTERFACE.md</code> · <code>docs/UPSTREAM.md</code></li>
    </ul>
  </div>

  <footer>baidu-service · 未登记 Host 一律 404；本页由服务自身渲染（无外部依赖）。</footer>
</div>
</body>
</html>
"""
