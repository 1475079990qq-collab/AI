import 'dart:async';

import 'package:shared_preferences/shared_preferences.dart';

/// 大脑服务地址配置。
///
/// 默认 `http://10.0.2.2:8000`（Android 模拟器访问宿主机）；
/// 真机需要在界面里改成局域网地址，例如 `http://192.168.1.5:8000`。
class AppConfig {
  static const String _prefKey = 'brain_url';
  static const String defaultUrl = 'http://10.0.2.2:8000';

  String _brainUrl = defaultUrl;
  bool _customized = false;

  /// 当前生效的地址（无末尾斜杠）。
  String get brainUrl => _brainUrl;

  /// 用户是否自己设过地址。false 说明还在用模拟器默认值，真机一定连不上，
  /// 界面要先引导填地址。
  bool get hasCustomUrl => _customized;

  /// 启动时从 shared_preferences 读取；无记录或非法值回落到默认值。
  Future<void> load() async {
    final SharedPreferences prefs = await SharedPreferences.getInstance();
    final String? saved = prefs.getString(_prefKey);
    if (saved != null && saved.isNotEmpty) {
      _brainUrl = _normalize(saved) ?? defaultUrl;
      _customized = _brainUrl != defaultUrl;
    }
  }

  /// 设置并持久化地址：自动去掉末尾斜杠、校验 http(s) 前缀。
  /// 非法地址抛 [ArgumentError]，由设置对话框提示用户。
  Future<void> setBrainUrl(String value) async {
    final String? normalized = _normalize(value);
    if (normalized == null) {
      throw ArgumentError('地址必须以 http:// 或 https:// 开头，例如 http://192.168.1.5:8000');
    }
    _brainUrl = normalized;
    _customized = true;
    final SharedPreferences prefs = await SharedPreferences.getInstance();
    await prefs.setString(_prefKey, normalized);
  }

  static String? _normalize(String raw) {
    String s = raw.trim();
    while (s.endsWith('/')) {
      s = s.substring(0, s.length - 1);
    }
    if (!s.startsWith('http://') && !s.startsWith('https://')) {
      return null;
    }
    final Uri? uri = Uri.tryParse(s);
    if (uri == null || uri.host.isEmpty) {
      return null;
    }
    return s;
  }
}
