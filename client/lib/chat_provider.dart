import 'dart:async';

import 'package:flutter/foundation.dart';

import 'audio_player.dart';
import 'config.dart';
import 'recorder.dart';
import 'sse_client.dart';
import 'system_shell.dart';

/// 服务端发布的新版本（只在比本机新时才生成）。
class UpdateInfo {
  const UpdateInfo({
    required this.versionName,
    required this.notes,
    required this.url,
  });

  final String versionName;
  final String notes;
  final String url;
}

/// 一次检查更新的结果：[reachable] 区分「确实是最新」和「连不上服务器」。
class UpdateCheck {
  const UpdateCheck({required this.reachable, this.info});

  final bool reachable;
  final UpdateInfo? info;
}

/// 一张递到手上的纸：后端从这一轮回答里摘出来的干货。
class DeliveryCard {
  const DeliveryCard({
    required this.kind,
    required this.title,
    required this.items,
    this.note = '',
  });

  final String kind;
  final String title;
  final List<String> items;
  final String note;

  /// 从 card 帧解析；标题或条目为空就当作没有卡（宁可不出，不出半张）。
  static DeliveryCard? fromEvent(SseEvent event) {
    final String title = (event.title ?? '').trim();
    final List<String> items = event.items;
    if (title.isEmpty || items.isEmpty) return null;
    return DeliveryCard(
      kind: event.kind ?? 'note',
      title: title,
      items: items,
      note: event.note ?? '',
    );
  }
}

/// 一条对话消息。助手消息的 [text] 会在流式过程中被逐字追加。
class ChatMessage {
  ChatMessage({
    required this.role,
    required this.text,
    this.emotion = '',
  });

  /// 'user' 或 'assistant'。
  final String role;
  String text;
  String emotion;

  /// 这一轮递出来的卡；null 表示这轮只有「说」。
  DeliveryCard? card;

  /// 对上号用的两个 id：卡片记录 id 与这一轮的 request_id。
  int deliveryId = 0;
  String requestId = '';

  /// 0 没评 / 1 对了 / -1 不对。
  int feedback = 0;
}

/// 会话状态机：ChangeNotifier，界面用 ListenableBuilder 监听。
class ChatProvider extends ChangeNotifier {
  ChatProvider(this._config) {
    serverUrl = _config.brainUrl;
    _client = SseClient(serverUrl);
    startProactivePolling();
  }

  final AppConfig _config;
  late SseClient _client;
  final CompanionAudioPlayer _audio = CompanionAudioPlayer();
  final CompanionRecorder _recorder = CompanionRecorder();

  // 身份统一从 AppConfig 取：记忆归属是全局唯一的一处，不在各端各写一份。
  // （App 以前写死 'u1'、Web 每次随机生成，结果同一个人有两套记忆。）
  static const String _userId = AppConfig.userId;
  static const String _sessionId = 'app';

  /// 刚递出一张卡：墨滴据此做一次「分裂出一枚薄片」的动作，1.2 秒后收回。
  bool cardJustArrived = false;
  Timer? _cardTimer;

  /// 全部消息（含正在生成的半句）。
  final List<ChatMessage> messages = <ChatMessage>[];
  bool isStreaming = false;

  /// 正在按住录音（UI 显示红色麦克风）。
  bool isRecording = false;

  /// 松手后正在等 /asr 返回（UI 显示转圈）。
  bool isRecognizing = false;

  String currentEmotion = 'neutral';
  String serverUrl = '';
  String? lastError;

  /// 还没设过地址（仍是模拟器默认值），界面上要先引导填服务器地址。
  bool get needsServerSetup => !_config.hasCustomUrl;

  bool _cancelled = false;
  bool _disposed = false;

  /// 主动开口的轮询定时器：它先说的话不是流的产物，只能问「有没有」。
  Timer? _proactiveTimer;

  /// 它可能先开口。启动 20 秒后问一次（让首批系统任务先跑完），之后每 5 分钟一次。
  /// 说不说话由后端决定（静默时段 / 每天上限 / 刚聊过都不说），客户端不做判断。
  void startProactivePolling() {
    _proactiveTimer?.cancel();
    _proactiveTimer = Timer.periodic(
      const Duration(minutes: 5),
      (_) => unawaited(pollProactive()),
    );
    Timer(const Duration(seconds: 20), () => unawaited(pollProactive()));
  }

  /// 问一次「它有没有先开口」。取走即标记已送达，不会重复弹。
  Future<void> pollProactive() async {
    if (_disposed || isStreaming) return;   // 正在对话时不插话
    try {
      final List<Map<String, dynamic>> items =
          await _client.proactive(userId: _userId);
      if (items.isEmpty) return;
      for (final Map<String, dynamic> it in items) {
        final String text = '${it['text'] ?? ''}'.trim();
        if (text.isEmpty) continue;
        messages.add(ChatMessage(role: 'assistant', text: text));
      }
      _safeNotify();
    } catch (_) {
      // 主动开口取不到就算了，绝不弹错误——它不该打断任何事
    }
  }

  /// 本轮收到的完整回复。先不进 UI——字幕要等语音开口那一刻才揭示，
  /// 否则字先刷完、声音在后面慢慢追，看起来就永远不同步。
  String _streamBuffer = '';

  /// 已经切成句子、交给 TTS 的位置。
  int _cutUpTo = 0;

  /// 本轮正在逐句揭示的助手消息。
  ChatMessage? _activeAssistant;

  /// 合成队列（还没生成音频）与播放队列（音频就绪、等播放）。
  final List<_SpeechJob> _synthQueue = <_SpeechJob>[];
  final List<_SpeechJob> _playQueue = <_SpeechJob>[];
  bool _synthBusy = false;
  bool _playBusy = false;

  /// 轮次号：换轮 / 取消之后，上一轮还在飞的合成结果直接丢弃。
  int _gen = 0;

  /// 用户手动点了「停止朗读」：本轮后续只出字、不出声。
  bool _mute = false;

  /// 正在播放语音的那条助手消息（界面用它播报指示）。
  ChatMessage? _speaking;

  /// 句末标点：一出现就可以切句。
  static const Set<String> _sentenceEnds = <String>{
    '。', '！', '？', '!', '?', '…', '；', ';', '\n',
  };

  /// 逗号类：句子太长时在这些地方断，比硬等句号更自然。
  static const Set<String> _softEnds = <String>{
    '，', '、', ',', '：', ':', '—', '~',
  };

  /// 第一句尽量短：用户发完消息后越快出声越好。
  static const int _firstChunkChars = 6;

  /// 之后的句子攒长一点再切。每多切一次就多一个拼接点，
  /// 一段段接起来听感就是「老是断」，所以宁可一句长一点、少切几次。
  static const int _minChunkChars = 18;

  /// 超过这个长度就允许在逗号处断开，免得一口气合成太久、首句出声太慢。
  static const int _softMaxChars = 40;

  /// 正在出声（界面显示「正在说…」和停止按钮）。
  bool get isSpeaking => _speaking != null;

  /// 正在被朗读的那条消息；界面据此高亮。
  ChatMessage? get speakingMessage => _speaking;

  void _safeNotify() {
    if (!_disposed) notifyListeners();
  }

  /// 按住麦克风：停掉正在播的 TTS（外放时会被麦克风收进去），然后开始录音。
  Future<bool> startRecording() async {
    if (isStreaming || isRecording || isRecognizing) return false;
    await _audio.stop();
    final bool ok = await _recorder.start();
    if (!ok) {
      lastError = '麦克风权限被拒绝，请在系统设置里允许本应用录音';
      _safeNotify();
      return false;
    }
    isRecording = true;
    _safeNotify();
    return true;
  }

  /// 松手：停止录音 → /asr → 识别出文字直接发出去。
  Future<void> stopRecordingAndRecognize() async {
    if (!isRecording) return;
    isRecording = false;
    isRecognizing = true;
    _safeNotify();
    try {
      final Uint8List? wav = await _recorder.stop();
      if (wav == null) {
        lastError = '没录到声音，按住多说一会儿';
      } else {
        final String text = await _client.asr(wav);
        if (text.isEmpty) {
          lastError = '没听清，再说一遍？';
        } else {
          unawaited(send(text));   // 先复位识别状态，别让整轮回复卡住按钮
        }
      }
    } catch (error) {
      lastError = '语音识别失败：$error';
    } finally {
      isRecognizing = false;
      _safeNotify();
    }
  }

  /// 墨滴刚递出东西：亮一次、分裂一次，然后收回。
  void _pulseDelivery() {
    cardJustArrived = true;
    _cardTimer?.cancel();
    _cardTimer = Timer(const Duration(milliseconds: 1200), () {
      cardJustArrived = false;
      _safeNotify();
    });
  }

  /// 点一下「对了 / 不对」。
  ///
  /// 只报结果、不写长文 —— 这是全系统最便宜的信号：一次点击换它下次少错一次。
  /// 上传失败不谎报成功（本地不留下一个假的「已记下」）。
  Future<void> rateMessage(ChatMessage msg, bool good) async {
    final int verdict = good ? 1 : -1;
    if (msg.feedback == verdict) return;
    msg.feedback = verdict;
    _safeNotify();
    final bool ok = await _client.feedback(
      requestId: msg.requestId,
      verdict: verdict,
      deliveryId: msg.deliveryId,
      target: msg.text,
    );
    if (!ok) {
      msg.feedback = 0;
      lastError = '反馈没送出去（大脑没连上？）';
      _safeNotify();
    }
  }

  /// 发送一条文本并消费 SSE 流。
  Future<void> send(String text) async {
    final String trimmed = text.trim();
    if (trimmed.isEmpty || isStreaming) return;

    lastError = null;
    _cancelled = false;
    _mute = false;
    _speaking = null;
    // 上一轮没揭示完的字先补齐，别让用户丢字。
    final ChatMessage? previous = _activeAssistant;
    if (previous != null) previous.text = _streamBuffer;
    _gen += 1;
    _streamBuffer = '';
    _cutUpTo = 0;
    _activeAssistant = null;
    _synthQueue.clear();
    _playQueue.clear();
    await _audio.stop();   // 上一轮没播完的话直接掐掉，避免两句话叠在一起
    messages.add(ChatMessage(role: 'user', text: trimmed));
    final ChatMessage assistant = ChatMessage(role: 'assistant', text: '');
    messages.add(assistant);
    isStreaming = true;
    cardJustArrived = false;
    _cardTimer?.cancel();
    currentEmotion = 'neutral';
    _safeNotify();

    try {
      await for (final SseEvent event in _client.chat(
        text: trimmed,
        userId: _userId,
        sessionId: _sessionId,
      )) {
        if (_cancelled) break;
        switch (event.type) {
          case 'token':
            // 只进缓冲，不动 UI；凑够一句就立刻送去合成。
            _streamBuffer += event.delta ?? '';
            _cutSentences(assistant, currentEmotion, force: false);
            break;
          case 'clarify':
            // 低信息量追问：整句替换，同样等语音开口时再揭示。
            final String? question = event.question;
            if (question != null && question.isNotEmpty) {
              _streamBuffer = question;
              _cutUpTo = 0;
            }
            final String? emotion = event.emotion;
            if (emotion != null) {
              currentEmotion = emotion;
              assistant.emotion = emotion;
            }
            _cutSentences(assistant, currentEmotion, force: true);
            break;
          case 'done':
            // 只有 done 才结束流；剩下的半句也切成一句，让语音带着字幕走。
            final String emotion = event.emotion ?? 'neutral';
            currentEmotion = emotion;
            assistant.emotion = emotion;
            _cutSentences(assistant, emotion, force: true);
            break;
          case 'error':
            // 出错就把已有的字全放出来，别让用户干等一个不会来的语音。
            lastError = event.message ?? event.code ?? '服务端返回错误';
            _revealAll(assistant);
            break;
          case 'meta':
            // 情感判断在开流前就做完了，情绪随首帧下来；这里必须立刻记住，
            // 否则流式过程中切出去的句子全按 neutral 合成，判断等于没用上。
            final String? metaEmotion = event.emotion;
            if (metaEmotion != null && metaEmotion.isNotEmpty) {
              currentEmotion = metaEmotion;
              assistant.emotion = metaEmotion;
              _safeNotify();
            }
            final String? rid = event.requestId;
            if (rid != null && rid.isNotEmpty) assistant.requestId = rid;
            break;
          case 'card':
            // 后端递出来一张纸：挂在消息上，并让墨滴做一次「分裂递出」的动作。
            final DeliveryCard? card = DeliveryCard.fromEvent(event);
            if (card != null) {
              assistant.card = card;
              assistant.deliveryId = event.deliveryId ?? 0;
              final String? rid = event.requestId;
              if (rid != null && rid.isNotEmpty) assistant.requestId = rid;
              _pulseDelivery();
              _safeNotify();
            }
            break;
          case 'ping':
          default:
            break;
        }
      }
    } catch (error) {
      // 断连 / 网络异常：复位状态，保留已收到的半句。
      lastError = '连接异常：$error';
    } finally {
      isStreaming = false;
      _safeNotify();
    }
  }

  /// 按句末标点把缓冲切成句子：每切出一句就排进合成队列。
  /// [force] 为 true 表示流已结束，剩下多少都当成最后一句。
  void _cutSentences(ChatMessage assistant, String emotion,
      {required bool force}) {
    _activeAssistant = assistant;
    while (true) {
      final int cut = _findCut(_cutUpTo);
      if (cut <= 0) break;
      _pushJob(cut, emotion);
      _cutUpTo = cut;
    }
    if (force && _cutUpTo < _streamBuffer.length) {
      _pushJob(_streamBuffer.length, emotion);
      _cutUpTo = _streamBuffer.length;
    }
  }

  /// 从 [from] 往后找第一个合适的断点：第一句短、后面的句子攒长，
  /// 实在等不到句号时允许在逗号处断开。找不到返回 -1（继续等下一个 token）。
  int _findCut(int from) {
    final bool first = from == 0;
    final int minChars = first ? _firstChunkChars : _minChunkChars;
    for (int i = from; i < _streamBuffer.length; i++) {
      final int len = i + 1 - from;
      final String ch = _streamBuffer[i];
      if (_sentenceEnds.contains(ch) && len >= minChars) return i + 1;
      if (!first && len >= _softMaxChars && _softEnds.contains(ch)) {
        return i + 1;
      }
    }
    return -1;
  }

  void _pushJob(int endIndex, String emotion) {
    final String chunk = _streamBuffer.substring(_cutUpTo, endIndex).trim();
    if (chunk.isEmpty) return;
    _synthQueue.add(_SpeechJob(
      text: chunk,
      endIndex: endIndex,
      emotion: emotion,
      gen: _gen,
    ));
    unawaited(_pumpSynth());
  }

  /// 合成与播放解耦：这边只管把句子变成音频，一句接一句，不等播放。
  /// 于是第二句在第一句还在播的时候就已经合好了，句间不留空档。
  Future<void> _pumpSynth() async {
    if (_synthBusy) return;
    _synthBusy = true;
    try {
      while (_synthQueue.isNotEmpty) {
        final _SpeechJob job = _synthQueue.removeAt(0);
        if (job.gen != _gen || _cancelled) continue;
        // 用户已点停止朗读：不再合成，只把字幕按节奏放出来。
        if (!_mute) job.audio = await _synthWithRetry(job);
        if (job.gen != _gen) continue;
        _playQueue.add(job);
        unawaited(_pumpPlay());
      }
    } finally {
      _synthBusy = false;
    }
  }

  /// 合成一句，失败重试一次。
  ///
  /// 以前失败就把这句静音跳过，听感上就是「说到一半突然没声了」，
  /// 而多数失败是一次性的（上游忙、连接抖动），补一次基本就回来了。
  Future<Uint8List?> _synthWithRetry(_SpeechJob job) async {
    for (int attempt = 0; attempt < 2; attempt++) {
      if (job.gen != _gen || _cancelled) return null;
      try {
        return await _client.tts(text: job.text, emotion: job.emotion);
      } catch (_) {
        if (attempt == 0) {
          await Future<void>.delayed(const Duration(milliseconds: 400));
        }
      }
    }
    return null;   // TTS 真的不可用：只出字，不出声。
  }

  /// 播放一段，同时把这一段字幕放到界面上：声音和文字同一时刻出现。
  Future<void> _pumpPlay() async {
    if (_playBusy) return;
    _playBusy = true;
    try {
      while (_playQueue.isNotEmpty) {
        final _SpeechJob job = _playQueue.removeAt(0);
        if (job.gen != _gen) continue;
        _reveal(job.endIndex);
        final Uint8List? audio = job.audio;
        if (audio == null || _cancelled || _mute) continue;
        _speaking = _activeAssistant;
        _safeNotify();
        await _audio.playWav(audio);
        // 队列里还有下一句时不要把播报状态摘掉，否则界面会闪一下。
        if (_playQueue.isEmpty) {
          _speaking = null;
          _safeNotify();
        }
      }
    } finally {
      _speaking = null;
      _playBusy = false;
      _safeNotify();
    }
  }

  /// 停止朗读：本轮后面的句子不再出声，但字幕照常往下走。
  Future<void> stopSpeaking() async {
    _mute = true;
    _synthQueue.clear();
    _playQueue.clear();
    _speaking = null;
    await _audio.stop();
    _safeNotify();
  }

  void _reveal(int endIndex) {
    final ChatMessage? msg = _activeAssistant;
    if (msg == null) return;
    final int end = endIndex < 0
        ? 0
        : (endIndex > _streamBuffer.length ? _streamBuffer.length : endIndex);
    msg.text = _streamBuffer.substring(0, end);
    _safeNotify();
  }

  /// 出错 / 取消时把已经收到的字一次性放出来。
  void _revealAll(ChatMessage msg) {
    msg.text = _streamBuffer;
    _safeNotify();
  }

  /// 查更新：本地 versionCode 由原生读取，和服务端 /app/version 比大小。
  /// 任何异常都当成「没更新」，绝不打断聊天。
  Future<UpdateCheck> checkUpdate() async {
    final Map<String, dynamic>? remote = await _client.appVersion();
    if (remote == null) {
      return const UpdateCheck(reachable: false);
    }
    final int latest = int.tryParse('${remote['version_code'] ?? 0}') ?? 0;
    final String path = '${remote['download_path'] ?? ''}'.trim();
    if (latest <= 0 || path.isEmpty) {
      return const UpdateCheck(reachable: true);
    }
    final AppVersion local = await SystemShell.appVersion();
    if (latest <= local.code) {
      return const UpdateCheck(reachable: true);
    }
    final String base = _client.baseUrl.replaceAll(RegExp(r'/+$'), '');
    return UpdateCheck(
      reachable: true,
      info: UpdateInfo(
        versionName: '${remote['version_name'] ?? ''}',
        notes: '${remote['notes'] ?? ''}',
        url: '$base$path',
      ),
    );
  }

  /// 修改并持久化服务器地址；非法地址抛 ArgumentError 由界面提示。
  Future<void> setServerUrl(String value) async {
    await _config.setBrainUrl(value);
    serverUrl = _config.brainUrl;
    _client.baseUrl = serverUrl;
    _safeNotify();
  }

  /// 取消当前流并复位 isStreaming（供「停止」按钮 / 断连使用）。
  void cancel() {
    if (!isStreaming) return;
    _cancelled = true;
    isStreaming = false;
    _gen += 1;   // 让还在飞的语音结果作废
    _synthQueue.clear();
    _playQueue.clear();
    _speaking = null;
    final ChatMessage? msg = _activeAssistant;
    if (msg != null) msg.text = _streamBuffer;   // 已经收到的字别丢
    unawaited(_audio.stop());
    _safeNotify();
  }

  /// 关掉错误条。
  void dismissError() {
    lastError = null;
    _safeNotify();
  }

  /// 清空会话。
  void clear() {
    _cancelled = true;
    _gen += 1;
    _synthQueue.clear();
    _playQueue.clear();
    _streamBuffer = '';
    _cutUpTo = 0;
    _activeAssistant = null;
    _speaking = null;
    _mute = false;
    messages.clear();
    isStreaming = false;
    currentEmotion = 'neutral';
    lastError = null;
    _safeNotify();
  }

  @override
  void dispose() {
    _disposed = true;
    _cancelled = true;
    _proactiveTimer?.cancel();
    _cardTimer?.cancel();
    unawaited(_audio.dispose());
    unawaited(_recorder.dispose());
    super.dispose();
  }
}

/// 一句待合成 / 待播出的台词。
class _SpeechJob {
  _SpeechJob({
    required this.text,
    required this.endIndex,
    required this.emotion,
    required this.gen,
  });

  /// 这一句的文本（送给 /tts）。
  final String text;

  /// 这句在 [_streamBuffer] 里的结束位置，播放时按它揭示字幕。
  final int endIndex;

  final String emotion;

  /// 所属轮次，用于丢弃过期结果。
  final int gen;

  /// 合成好的 WAV；null 表示 TTS 不可用，只出字。
  Uint8List? audio;
}
