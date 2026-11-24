# Phone Gallery Cleaner

一个本地运行的 Android 手机相册整理工具：插上手机自动扫描相册，按相册 / 月份 / 截图来源应用分类，支持拖拽框选、键盘操作、视频播放，以及「移到电脑回收站」和「永久删除」两种清理方式。

所有处理都在你自己电脑上完成，**不上传任何图片或数据到网络**。

## 功能

- **原生软件窗口**：双击 exe 直接弹出应用窗口，不打开浏览器、不需要命令行
- 自动读取 Android 媒体库（图片 + 视频），生成缩略图墙
- 分类维度：相册 / 文件夹、拍摄月份、截图来源应用、类型（相机 / 截图 / 视频 / 其他），维度间互斥切换，侧栏数字即筛选结果数
- 截图按包名识别来源应用，例如微信、美团、美团外卖、淘宝等（映射表在 `gallery_core.py` 的 `APP_NAMES`，可自行补充）
- **视频播放**：点开视频按需从手机拉到本地缓存，支持拖动进度条；删除后自动清缓存
- 可疑重复检测：同尺寸同大小、同一时刻连拍截图
- 点选、按住左键拖拽框选；大图预览支持键盘操作
- 删除分两档：
  - **移到电脑回收站**：先复制到本地 `recycle/` 并校验大小，成功后才从手机移除，可恢复
  - **永久删除**：需二次确认，每 10 个一批执行并逐个校验，不可恢复

## 下载即用（推荐）

到 [Releases](../../releases) 下载 `PhoneGalleryCleaner.exe`，双击即可运行：

1. 手机开启「USB 调试」，数据线连接电脑，在手机上点「允许 USB 调试」
2. 双击 `PhoneGalleryCleaner.exe`
3. 应用窗口立即打开，后台自动扫描手机，完成后显示相册墙
4. 点右上角「退出程序」关闭

exe 内置了 adb 及其依赖 DLL、页面模板，换电脑无需安装 Python 或 adb。运行数据（缩略图、视频缓存、回收站备份、删除日志）写在 exe 同目录；若目录不可写，自动改用 `LocalAppData\PhoneGalleryCleaner`。

首次启动需要拉取缩略图，视数量可能需要几十秒；之后刷新只拉新增文件，通常几秒完成。

## 操作说明

| 操作 | 效果 |
| --- | --- |
| 单击卡片 | 打开大图预览 / 播放视频（不会误选） |
| 按住左键拖动 | 框选经过的图片 |
| 从已选中卡片起拖 | 取消经过图片的选中 |
| 大图预览 `←` / `→` 或 `A` / `D` | 上一张 / 下一张 |
| 大图预览 `空格` | 勾选 / 取消当前图片，预览不关闭 |
| 大图预览 `Del` / `Backspace` | 删除选中项（默认走「移到回收站」确认流程） |
| 大图预览 `Esc` | 关闭预览并停止播放 |

永久删除请使用页面底部按钮，并勾选确认项。

## 从源码运行

环境要求：

- Windows 10/11、Python 3.9+、Android 手机（开启 USB 调试）
- `adb.exe` 在 PATH 中、常见位置，或写在 `config.json` 里

```bash
pip install pywebview
python server.py
```

应用窗口会自动打开。可选配置见 `config.example.json`（`serial` 留空表示自动选择第一台已授权设备）。

## 自行打包 exe

```bash
pip install pyinstaller pywebview
pyinstaller --onefile --noconsole --name PhoneGalleryCleaner \
  --hidden-import=webview.platforms.winforms \
  --add-data "template.html;." --add-data "adb.exe;." \
  --add-data "AdbWinApi.dll;." --add-data "AdbWinUsbApi.dll;." \
  server.py
```

需先把 `adb.exe`、`AdbWinApi.dll`、`AdbWinUsbApi.dll` 放到源码目录（从 Android SDK platform-tools 获取）。

## 文件说明

- `gallery_core.py`：扫描逻辑、媒体库查询、缩略图拉取、分类与重复标记
- `server.py`：本地 HTTP 服务、原生窗口、删除与视频接口
- `template.html`：前端页面模板
- `config.example.json`：可选的 adb 路径配置示例

## 隐私与安全

- 不使用任何外部网络服务；服务只监听 `127.0.0.1`，不对外暴露
- 运行时生成的 `data.json`、`index.html`、`images.txt`、`videos.txt`、`thumbs/`、`video_cache/`、`recycle/`、`delete_log.jsonl` 都含本地数据，**已在 `.gitignore` 中排除，不会进入仓库**
- 删除有路径白名单：仅允许 `/sdcard/DCIM`、`Pictures`、`Download`、`Movies` 下的文件，拒绝路径穿越；扫描期间禁止删除；单次上限 500 项
- 「移到电脑回收站」模式保留备份，`recycle/时间戳/manifest.json` 记录了原始路径，可据此恢复
- 永久删除不可恢复，请自行确认选中项；作者不对误删负责

## 许可证

MIT，见 [LICENSE](LICENSE)。
