import 'package:flutter/services.dart';

/// 按住说话录音：走原生 AudioRecord（16k / 单声道 / 16-bit PCM WAV）。
///
/// 不用第三方录音插件：它们依赖的 CMake 原生库在中文项目路径下会构建失败，
/// 原生通道只输出后端 /asr 认的 wav，零转码。
class CompanionRecorder {
  static const MethodChannel _channel = MethodChannel('companion/recorder');
  bool _started = false;

  /// 开始录音；没有麦克风权限时返回 false（由上层提示用户）。
  Future<bool> start() async {
    try {
      final bool ok = await _channel.invokeMethod<bool>('start') ?? false;
      _started = ok;
      return ok;
    } on PlatformException {
      _started = false;
      return false;
    }
  }

  /// 停止并返回 WAV 字节；没录到 / 太短（<1KB）返回 null。
  Future<Uint8List?> stop() async {
    if (!_started) return null;
    _started = false;
    try {
      final Uint8List? bytes = await _channel.invokeMethod<Uint8List>('stop');
      if (bytes == null || bytes.length < 1000) return null;
      return bytes;
    } on PlatformException {
      return null;
    }
  }

  /// 放弃当前录音（页面销毁等场景）。
  Future<void> cancel() async {
    if (!_started) return;
    _started = false;
    try {
      await _channel.invokeMethod<void>('cancel');
    } catch (_) {
      // 忽略。
    }
  }

  Future<void> dispose() async {
    await cancel();
  }
}
