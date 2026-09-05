# NekroAgent 媒体阅读器

> 处理 OneBot 音频和视频消息，并整合 Bilibili 分享链接解析、下载与媒体分析能力。

## 快速开始

将整个 `nekro_plugin_media` 目录复制到 NekroAgent 数据目录的插件工作区：

```text
DATA_DIR/plugins/workdir/nekro_plugin_media/
```

确认目录中包含 `__init__.py`，然后按照 NekroAgent 的插件加载流程启动。插件只支持 OneBot V11 适配器。

## 插件结构

```text
nekro_plugin_media/
├── __init__.py       # 插件实例、配置与包导出
├── locator.py        # 媒体消息定位
├── matcher.py        # 音视频消息拦截器
├── parser.py         # 媒体解析流程
├── media_io.py       # 文件读取和 ffmpeg 转换
├── gemini.py         # Gemini API 客户端
├── prompts.py        # 媒体分析提示词
├── cache.py          # 媒体句柄缓存
├── sandbox.py        # analyze_media_file 沙盒方法
├── commands.py       # 管理命令
├── lifecycle.py      # 生命周期清理
├── types.py          # 媒体类型和处理状态
└── bilibili/         # Bilibili 子模块，不是独立 Nekro 插件
```

## 功能说明

- Bilibili 链路使用优先级 `9`，识别分享链接并替换为视频消息，同时注入标题、来源、简介和热评。
- 媒体分析链路使用优先级 `10`，为音频和视频建立句柄，按需调用 `analyze_media_file`。
- 音频解析同步返回结果；视频解析进入后台任务，完成后向原聊天推送结果。
- 缓存、临时文件和 Bilibili 下载任务可通过 `clear_media` 命令清理。
- Bilibili 已并入本插件，`bilibili/` 不包含第二个 `NekroPlugin` 实例。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `GEMINI_API_KEY` | 空 | Gemini API Key，使用媒体分析时必须配置 |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Gemini 模型名称 |
| `download_quality` | `480p` | Bilibili 下载画质 |
| `max_duration` | `30` | Bilibili 视频最大时长，单位为分钟 |
| `desc_char_limit` | `150` | Bilibili 简介截断长度 |
| `comment_count` | `3` | 注入的 Bilibili 热评数量 |
| `comment_char_limit` | `100` | 单条热评截断长度 |
| `NAPCAT_TEMP_DIR` | `/app/.config/QQ/NapCat/temp` | NapCat 临时文件目录 |

## 外部依赖

- NekroAgent 和 OneBot V11 适配器。
- Gemini API Key，以及可访问 Gemini API 的网络环境。
- `ffmpeg`，用于部分音视频转换。
- `yt-dlp`，用于 Bilibili 视频下载；部署时需要确保插件运行环境可以安装或提供该依赖。
- 可选的 `bilibili_cookies.txt`，放在插件数据目录中，用于需要登录态的下载场景。

## 沙盒方法与命令

### `analyze_media_file`

根据媒体句柄提取音视频内容。音频会同步返回解析结果，视频会返回提交状态，并在后台任务完成后推送结果。

### `clear_media`

超级用户命令，清理媒体和 Bilibili 临时文件，并取消未完成的下载任务。

## 开发

插件入口在 `__init__.py` 中定义并导出 `plugin` 实例，Bilibili 注册模块在包加载时由入口加载。修改后可执行：

```powershell
python -m py_compile *.py bilibili\*.py
```

完整行为需要真实的 NekroAgent、OneBot V11、Gemini、ffmpeg、yt-dlp 和 NapCat 环境验证。

## 相关资源

- [NekroAgent 官方文档](https://doc.nekro.ai/)
- [插件开发快速上手](https://doc.nekro.ai/docs/04_plugin_dev/01_quick_start.html)
- [Nekro 插件模板](https://github.com/KroMiose/nekro-plugin-template)

## 许可证

本项目当前未单独声明许可证。
