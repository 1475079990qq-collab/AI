# companion 安卓客户端（Flutter）

包名 `companion_client`，Android `applicationId com.companion.app`，`minSdk` 用 Flutter 默认值 24（Android 7.0+）。
客户端零推理零记忆，只做四件事：聊天 UI、`/chat` 的 SSE 解析、`/tts` 的 WAV 播放、按住说话走 `/asr`。

## 本期功能

- 键盘文本输入 + **按住麦克风说话**：原生 `AudioRecord` 录 16k 单声道 16-bit PCM WAV → `POST /asr` → 识别文字直接发送。
- 录音前自动停掉正在播放的 TTS，避免外放时把 AI 的声音录进去。
- `/chat` 逐 token 显示、情绪徽标、`done` 后异步取 `/tts` 播放。
- 不做唤醒词、不做 ESP32。
- `error` 帧保留已生成的半句、显示错误条与「重试」，**不自动重试**；「停止」按钮与断连都会复位流式状态。

## 依赖

`http`、`just_audio`、`shared_preferences`、`path_provider`、`cupertino_icons`。

录音不用第三方插件：`record` 等插件带 CMake 原生库，在本项目的中文路径
（`E:\情感开发`）下构建会失败（`MalformedJsonException` / Kotlin 缓存路径乱码）。
因此录音由 `MainActivity.kt` 的 `companion/recorder` MethodChannel 直接实现，
输出后端 `/asr` 认的 16-bit PCM WAV。

同理，`path_provider_android` / `shared_preferences_android` 固定在不依赖 `jni`
的旧版本（见 `pubspec.yaml` 的 `dependency_overrides`）。

## 构建与安装

工具链已装在本机英文路径：

| 组件 | 路径 |
|---|---|
| Flutter 3.50.0 / Dart 3.14.0 | `E:\toolchains\flutter` |
| JDK 17 (Temurin) | `E:\toolchains\jdk-17` |
| Android SDK（platform 36 / build-tools 36.0.0） | `E:\toolchains\android-sdk` |

构建（PowerShell）：

```powershell
. E:\情感开发\.toolchains\env.ps1
cd E:\情感开发\companion\client
flutter build apk --debug
```

产物：`build\app\outputs\flutter-apk\app-debug.apk`，
同时复制了一份到 `companion-debug.apk` 方便传手机。

安装：

```powershell
adb install -r E:\情感开发\companion\client\companion-debug.apk
```

没有 USB 就把 APK 拷到手机点安装（debug 包，需允许「安装未知来源」）。

### 中文路径相关的规避

项目在 `E:\情感开发`（含中文），Flutter/AGP 对非 ASCII 路径有几处坑，已做如下处理：

- `android/gradle.properties`：`android.overridePathCheck=true`（AGP 路径检查）
- 同上：`kotlin.incremental=false` / `kotlin.incremental.java=false`（Kotlin 增量缓存无法在中文路径关闭）
- 工具链全部放英文路径（Flutter / JDK / Android SDK）
- 录音不用带 CMake 的插件（见「依赖」）

如果 `flutter.bat` 卡住不动（本机会卡在批处理锁上），绕过批处理直接用 Dart 快照：

```powershell
& E:\toolchains\flutter\bin\cache\dart-sdk\bin\dart.exe `
  --packages="E:\toolchains\flutter\packages\flutter_tools\.dart_tool\package_config.json" `
  "E:\toolchains\flutter\bin\cache\flutter_tools.snapshot" build apk --debug
```

## 服务器地址怎么填

界面右上角「以太网」图标 / 顶部菜单「服务器地址」都能改，地址写入 `shared_preferences`，
自动去掉末尾斜杠并校验 `http(s)://` 前缀。

| 场景 | 填什么 |
|---|---|
| Android 模拟器 | `http://10.0.2.2:8000` |
| 真机 / 同一 WiFi | 本机当前是 `http://192.168.10.15:8000`（以 `ipconfig` 为准） |

真机还需要：

- Windows 防火墙放行 **8000 端口入站**。
- 手机与电脑在同一局域网，且未开启 AP/客户端隔离。
- `AndroidManifest.xml` 已设 `android:usesCleartextTraffic="true"`（Android 9+ 明文 HTTP 必需）。

后端必须监听 `0.0.0.0:8000`（`2-启动大脑.bat` 已如此），只监听 127.0.0.1 时手机连不上。

## Android 宿主工程

`android/` 已用 Flutter 3.50 模板重新生成（AGP 9.3.1 / Kotlin 2.4.20 / Gradle 9.5.0，
Gradle 发行版走腾讯镜像），并保留了：

- `applicationId com.companion.app`、`namespace com.companion.app`
- `AndroidManifest.xml` 的 `usesCleartextTraffic="true"` 与 `RECORD_AUDIO` 权限
- `MainActivity.kt`（原生录音 MethodChannel）

如果以后升级 Flutter SDK，重新执行
`flutter create --platforms android --org com.companion --project-name companion_client .`
后，请把上面三项与 `gradle.properties` 里的中文路径开关补回来。

## 验证状态

- `flutter pub get`：通过
- `flutter analyze`：0 error（余下 4 条 warning/info 为既有代码提示）
- `flutter build apk --debug`：通过
- 真机运行：**未验证**（本机没有连接 Android 设备）。语音链路（录音 → `/asr` → `/chat` → `/tts`）
  需要在手机上按一遍：允许麦克风权限 → 按住说话 → 松开后应直接发出识别文本。
