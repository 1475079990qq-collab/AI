import 'package:flutter/services.dart';

/// 当前安装包版本（由原生 PackageManager 读取，和 pubspec 的 version 一致）。
class AppVersion {
  const AppVersion({required this.code, required this.name});

  final int code;
  final String name;
}

/// 原生能力的小出口：读安装包版本、交给系统浏览器打开链接。
/// 走自有 MethodChannel，不引第三方插件——这个工程的构建环境对插件很挑。
class SystemShell {
  static const MethodChannel _channel = MethodChannel('companion/shell');

  /// 读当前安装包版本；拿不到时返回 code=0（宁可漏报，也不误报有新版）。
  static Future<AppVersion> appVersion() async {
    try {
      final Map<Object?, Object?>? raw =
          await _channel.invokeMapMethod<Object?, Object?>('appVersion');
      final int code = int.tryParse('${raw?['code'] ?? 0}') ?? 0;
      return AppVersion(code: code, name: '${raw?['name'] ?? ''}');
    } catch (_) {
      return const AppVersion(code: 0, name: '');
    }
  }

  /// 用系统浏览器打开链接（下载 APK 也走它）。
  static Future<bool> openUrl(String url) async {
    try {
      final bool? ok =
          await _channel.invokeMethod<bool>('openUrl', <String, Object?>{'url': url});
      return ok ?? false;
    } catch (_) {
      return false;
    }
  }
}
