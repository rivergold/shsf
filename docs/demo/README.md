# SHSF 交互演示

[index.html](index.html) 是一个单文件静态演示，复用 SHSF 当前前端和内置示例数据，不需要 Python 后端、数据库、模型或密钥。模拟论文与报告不对应真实研究结论。

## 本地查看

下载 `index.html` 后，直接用浏览器打开。也可以在仓库根目录启动静态文件服务：

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory docs
```

打开 <http://127.0.0.1:8000/demo/>。

可以体验每日推荐、摘要语言切换、阅读反馈、兴趣论文选择、精读记录搜索与收藏，以及打开演示报告。操作只影响当前页面内存，刷新即重置；演示不会登录账户、导入 Zotero 数据或生成真实报告。

## 发布到 GitHub Pages

目前只准备了静态文件，没有启用或发布 GitHub Pages。

在仓库的 **Settings → Pages** 中选择 **Deploy from a branch**，分支选择 `main`，目录选择 `/docs` 并保存。成功部署后，演示通常位于 `https://rivergold.github.io/shsf/demo/`，以 GitHub Pages 显示的实际地址为准。

确认页面能访问后，再把该地址添加到仓库 About 的 Website 和 README 中作为在线演示入口。发布方式参见 [GitHub Pages 官方说明](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)。

## 更新演示

前端页面改动后，在仓库根目录重新生成：

```sh
python3 scripts/build_demo.py
```

- `shsf/static/`：正式应用前端，作为演示的界面来源。
- `design/demo/data.json`：可公开的模拟数据。
- `design/demo/runtime.js`：本地模拟操作，不请求后端。
- `docs/demo/index.html`：生成的单文件页面，可直接作为静态网站部署。

按仓库的 [AGPL-3.0-only](../../LICENSE) 许可证提供。
