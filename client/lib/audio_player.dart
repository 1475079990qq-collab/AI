import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';

/// 播放 /tts 返回的 WAV 字节。
///
/// just_audio 没有官方 `BytesSource`（那是 audioplayers 的 API），因此把字节
/// 写入临时文件后用官方的 `AudioPlayer.setFilePath()` 播放。
/// 任何异常都静默降级：TTS 不可用或播放失败都不影响聊天 UI。
class CompanionAudioPlayer {
  CompanionAudioPlayer() : _player = AudioPlayer();

  final AudioPlayer _player;
  int _seq = 0;
  File? _lastFile;

  /// 是否正在出声（界面用它决定要不要显示「停止朗读」）。
  bool get isPlaying => _player.playing;

  /// 播放一段 WAV 字节；失败静默降级。
  ///
  /// 结束判定不看 `play()` 的返回值：它在各平台 / 各版本上语义不一致，
  /// 有的「开始播」就返回，有的「被暂停」也返回。这里直接盯 processingState，
  /// 只有真的到 completed 才算播完，超时（音频焦点被抢、解码卡住）就主动收尾，
  /// 否则播放链会一直堵在这句上，听起来就是「说到一半断了、后面没声」。
  Future<void> playWav(Uint8List bytes) async {
    try {
      final Directory dir = await getTemporaryDirectory();
      _seq += 1;
      final File file = File('${dir.path}/companion_tts_$_seq.wav');
      await file.writeAsBytes(bytes, flush: true);
      await _player.setFilePath(file.path);

      final int ms = _wavMillis(bytes);
      final Duration limit =
          Duration(milliseconds: (ms > 0 ? ms : 30000) + 5000);
      final Future<void> done = _player.processingStateStream
          .firstWhere((ProcessingState s) => s == ProcessingState.completed)
          .then((_) {});
      unawaited(_player.play());
      try {
        await done.timeout(limit);
      } on TimeoutException {
        await _player.stop();
      }

      // 上一句的临时文件留到这一句播完再删，避免个别机型边播边删出问题。
      final File? stale = _lastFile;
      _lastFile = file;
      if (stale != null && stale.path != file.path) {
        unawaited(stale.delete().then<void>((_) {}).catchError((Object _) {}));
      }
    } catch (_) {
      // 静默降级，不让 UI 崩。
    }
  }

  /// 停止当前播放（开始录音 / 用户点停止时调用）。
  Future<void> stop() async {
    try {
      await _player.stop();
    } catch (_) {
      // 静默降级。
    }
  }

  Future<void> dispose() async {
    try {
      final File? last = _lastFile;
      await _player.dispose();
      if (last != null) {
        unawaited(last.delete().then<void>((_) {}).catchError((Object _) {}));
      }
    } catch (_) {
      // 忽略。
    }
  }

  /// 从 16bit PCM WAV 头里读时长（毫秒）。读不出来返回 0，由调用方兜底。
  int _wavMillis(Uint8List b) {
    if (b.length < 44) return 0;
    int? byteRate;
    int pos = 12;
    while (pos + 8 <= b.length) {
      final String id = String.fromCharCodes(b.sublist(pos, pos + 4));
      final int size = _u32(b, pos + 4);
      if (id == 'fmt ' && pos + 16 <= b.length) {
        byteRate = _u32(b, pos + 16);
      } else if (id == 'data') {
        final int rate = byteRate ?? 0;
        if (rate <= 0) return 0;
        return (size * 1000) ~/ rate;
      }
      if (size < 0) break;
      pos += 8 + size + (size.isOdd ? 1 : 0);
    }
    return 0;
  }

  int _u32(Uint8List b, int offset) {
    if (offset + 4 > b.length) return 0;
    return b[offset] |
        (b[offset + 1] << 8) |
        (b[offset + 2] << 16) |
        (b[offset + 3] << 24);
  }
}
