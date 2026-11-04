"""Build a single-file, offline preview using the current SHSF frontend."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build():
    static = ROOT / 'shsf/static'
    template = (static / 'index.html').read_text()
    css = (static / 'style.css').read_text()
    app = (static / 'app.js').read_text().replace('/#agent', '#agent')
    runtime = (ROOT / 'design/demo/runtime.js').read_text()
    data = json.loads((ROOT / 'design/demo/data.json').read_text())
    payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    page = template.replace('__SHSF_TOKEN__', 'offline-demo')
    page = page.replace('<title>SHSF · 每日推荐</title>', '<title>SHSF · 交互演示</title>')
    page = page.replace('<link rel="stylesheet" href="/style.css">', '<style>'+css+'</style>')
    page = page.replace('<script src="/app.js" defer></script>', '')
    page = page.replace('href="/"', 'href="#feed"').replace('href="/#agent"', 'href="#agent"')
    banner = '''<aside class="demo-banner" aria-label="演示说明"><strong>SHSF 交互演示</strong><span>示例论文、推荐分数与报告均为模拟数据 · 操作仅在本页内存中生效</span><a href="https://github.com/rivergold/shsf" target="_blank" rel="noreferrer">查看源码 ↗</a><small id="demo-message" role="status"></small></aside>'''
    extra = '''<style>.demo-banner{padding:12px 24px;background:var(--soft);color:var(--green);display:flex;justify-content:center;align-items:center;gap:8px 18px;flex-wrap:wrap;font-size:12px}.demo-banner small:empty{display:none}.demo-banner small{flex-basis:100%;text-align:center}input:disabled{opacity:.6}</style>'''
    page = page.replace('<body>', '<body>'+extra+banner)
    scripts = '<script type="application/json" id="demo-data">'+payload+'</script><script>'+runtime+'</script><script>'+app+'</script>'
    page = page.replace('</body>', scripts+'</body>')
    destination = ROOT / 'docs/demo/index.html'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page)
    print('Built docs/demo/index.html (offline; no backend required)')


if __name__ == '__main__':
    build()
