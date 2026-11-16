import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'chat_provider.dart';
import 'config.dart';
import 'ink_drop.dart';
import 'system_shell.dart';
import 'theme.dart';

// ---- 配色：米白纸感 + 墨色 + 一处陶土橙 ----
// 全部来自 theme.dart；这里只是给旧代码留的别名，不要在别处再写死色值。
const Color _bgTop = Palette.paper;
const Color _bgBottom = Palette.paperDeep;
const Color _surface = Palette.card;
const Color _textPrimary = Palette.textPrimary;
const Color _textMuted = Palette.textMuted;
const Color _accent = Palette.terracotta;
const Color _danger = Color(0xFFC05A4D);

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final AppConfig config = AppConfig();
  await config.load();
  runApp(CompanionApp(config: config));
}

class CompanionApp extends StatefulWidget {
  const CompanionApp({super.key, required this.config});

  final AppConfig config;

  @override
  State<CompanionApp> createState() => _CompanionAppState();
}

class _CompanionAppState extends State<CompanionApp> {
  late final ChatProvider _chat;

  @override
  void initState() {
    super.initState();
    _chat = ChatProvider(widget.config);
  }

  @override
  void dispose() {
    _chat.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'companion',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.light,
        colorScheme: const ColorScheme.light(
          primary: Palette.terracotta,
          surface: Palette.card,
          error: _danger,
        ),
        scaffoldBackgroundColor: Palette.paper,
        splashFactory: NoSplash.splashFactory,
        // 衬线只在标题/正文大段文字上启用；控件仍是无衬线，长读不累。
        fontFamilyFallback: const <String>['Noto Serif CJK SC', 'sans-serif'],
      ),
      home: ChatScreen(chat: _chat),
    );
  }
}

/// 情绪 → 玩偶表情 + 一句小字。后端用 8 类英文标签，历史版本用过另一套，
/// 两套都认，认不出就当平静。
class _Mood {
  const _Mood(this.label, this.tint, this.smile);

  final String label;
  final Color tint;

  /// -1（嘴角向下）～ 1（笑开）
  final double smile;
}

_Mood _moodOf(String emotion) {
  switch (emotion) {
    case 'joy':
    case 'happy':
      return const _Mood('有点开心', Color(0xFFF0B36B), 0.95);
    case 'sad':
      return const _Mood('有点难过', Color(0xFF7FA3D7), -0.85);
    case 'anger':
    case 'angry':
      return const _Mood('有点生气', Color(0xFFE0705F), -0.5);
    case 'fear':
      return const _Mood('有点担心', Color(0xFF9B8BD0), -0.3);
    case 'surprise':
    case 'surprised':
      return const _Mood('有点惊讶', Color(0xFFE8C15C), 0.15);
    case 'disgust':
      return const _Mood('不太舒服', Color(0xFF8FA86B), -0.6);
    case 'trust':
    case 'care':
      return const _Mood('很安心', Color(0xFFE7A88B), 0.7);
    default:
      return const _Mood('安安静静', Color(0xFFD8BFA2), 0.35);
  }
}

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, required this.chat});

  final ChatProvider chat;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();

  /// 用户是否还停在底部：是的话新内容自动跟着滚，翻历史时不打扰。
  bool _pinned = true;

  /// 待安装的新版本；null 表示当前没有更新可提示。
  UpdateInfo? _update;
  Timer? _updateTimer;

  /// 最近一次连服务器失败。true 时顶部常驻「点这里设置地址」的提示条。
  bool _offline = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _scroll.addListener(() {
      if (!_scroll.hasClients) return;
      final double gap =
          _scroll.position.maxScrollExtent - _scroll.position.pixels;
      _pinned = gap < 120;
    });
    // 启动先查一次，之后每 5 分钟查一次：服务端发布新包后，开着的 App
    // 也会自己冒出更新条，不需要重启。
    unawaited(_checkUpdate());
    _updateTimer = Timer.periodic(
      const Duration(minutes: 5),
      (_) => unawaited(_checkUpdate()),
    );
    // 还没设过服务器地址，真机一定连不上，首帧后先弹一次设置。
    if (widget.chat.needsServerSetup) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) unawaited(_openServerDialog(context));
      });
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // 从后台切回来立刻查一次，这是最像「推送」的时机。
    if (state == AppLifecycleState.resumed) unawaited(_checkUpdate());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _updateTimer?.cancel();
    _scroll.dispose();
    _input.dispose();
    super.dispose();
  }

  /// 贴在底部时才自动跟随新内容。
  void _follow() {
    if (!_pinned) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.jumpTo(_scroll.position.maxScrollExtent);
    });
  }

  Future<void> _checkUpdate() async {
    final UpdateCheck result = await widget.chat.checkUpdate();
    if (!mounted) return;
    setState(() => _offline = !result.reachable);
    if (result.info == null) return;
    setState(() => _update = result.info);
  }

  Future<void> _manualCheck() async {
    final UpdateCheck result = await widget.chat.checkUpdate();
    if (!mounted) return;
    if (result.info != null) {
      setState(() => _update = result.info);
      return;
    }
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(result.reachable ? '已经是最新版本' : '连不上服务器，稍后再试',
            style: const TextStyle(fontSize: 12)),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  void _installUpdate(UpdateInfo info) {
    // 交给系统浏览器下载 APK；装的时候需要允许「安装未知应用」。
    unawaited(SystemShell.openUrl(info.url));
    setState(() => _update = null);
  }

  void _submit() {
    final String text = _input.text.trim();
    if (text.isEmpty || widget.chat.isStreaming) return;
    HapticFeedback.selectionClick();
    _input.clear();
    _pinned = true;
    unawaited(widget.chat.send(text));
  }

  // ---------------- 顶部 ----------------

  String _statusText(ChatProvider chat) {
    if (chat.isRecording) return '在听你说';
    if (chat.isRecognizing) return '正在听懂';
    if (chat.isStreaming) return '在想着怎么回你';
    if (chat.isSpeaking) return '正在说给你听';
    return '一直在';
  }

  Widget _topBar(ChatProvider chat) {
    final bool live = chat.isStreaming || chat.isSpeaking || chat.isRecording;
    return Padding(
      padding: const EdgeInsets.fromLTRB(22, 10, 14, 10),
      child: Row(
        children: <Widget>[
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              const Text(
                '小 暖',
                style: TextStyle(
                  fontSize: 15,
                  letterSpacing: 2,
                  color: _textPrimary,
                  fontWeight: FontWeight.w500,
                ),
              ),
              const SizedBox(height: 4),
              Row(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  _LiveDot(active: live),
                  const SizedBox(width: 6),
                  Text(
                    _statusText(chat),
                    style: const TextStyle(fontSize: 10.5, color: _textMuted),
                  ),
                ],
              ),
            ],
          ),
          const Spacer(),
          _IconTap(
            icon: Icons.more_horiz_rounded,
            onTap: () => _openMenu(context),
          ),
        ],
      ),
    );
  }

  /// 连不上服务器时常驻的提示条：点一下直接改地址，这是最容易漏的一步。
  Widget _offlineBanner() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 2, 20, 8),
      child: GestureDetector(
        onTap: () => _openServerDialog(context),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 10),
          decoration: BoxDecoration(
            color: _danger.withValues(alpha: 0.10),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: _danger.withValues(alpha: 0.30)),
          ),
          child: Row(
            children: <Widget>[
              const Icon(Icons.wifi_off_rounded, size: 15, color: _danger),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  '连不上服务器 · 点这里设置地址',
                  style: TextStyle(
                    fontSize: 11.5,
                    color: _danger.withValues(alpha: 0.95),
                  ),
                ),
              ),
              Text(
                widget.chat.serverUrl.replaceFirst('http://', ''),
                style: const TextStyle(fontSize: 10, color: _textMuted),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _updateBanner(UpdateInfo info) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 2, 20, 8),
      child: GestureDetector(
        onTap: () => _installUpdate(info),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 10),
          decoration: BoxDecoration(
            color: _accent.withValues(alpha: 0.10),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: _accent.withValues(alpha: 0.30)),
          ),
          child: Row(
            children: <Widget>[
              const Icon(Icons.arrow_circle_down_rounded,
                  size: 15, color: _accent),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  info.versionName.isEmpty
                      ? '有新版本，点这里更新'
                      : '有新版本 ${info.versionName}，点这里更新',
                  style: const TextStyle(fontSize: 11.5, color: _accent),
                ),
              ),
              GestureDetector(
                onTap: () => setState(() => _update = null),
                child: const Padding(
                  padding: EdgeInsets.only(left: 10),
                  child: Text('稍后',
                      style: TextStyle(fontSize: 11, color: _textMuted)),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  // ---------------- 形象区 ----------------

  Widget _presenceCard(ChatProvider chat) {
    final _Mood mood = _moodOf(chat.currentEmotion);
    final bool talking = chat.isSpeaking || chat.isStreaming;
    return Container(
      height: 214,
      margin: const EdgeInsets.only(top: 2, bottom: 22),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(26),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: <Color>[
            Color.lerp(_surface, mood.tint, 0.13)!,
            Color.lerp(_bgBottom, mood.tint, 0.07)!,
          ],
        ),
        border: Border.all(color: Colors.white.withValues(alpha: 0.05)),
        boxShadow: <BoxShadow>[
          BoxShadow(
            color: mood.tint.withValues(alpha: 0.10),
            blurRadius: 44,
            offset: const Offset(0, 20),
          ),
        ],
      ),
      child: Stack(
        children: <Widget>[
          // 背后的暖光
          Positioned.fill(
            child: DecoratedBox(
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(26),
                gradient: RadialGradient(
                  center: const Alignment(0, -0.35),
                  radius: 0.85,
                  colors: <Color>[
                    mood.tint.withValues(alpha: 0.16),
                    mood.tint.withValues(alpha: 0.0),
                  ],
                ),
              ),
            ),
          ),
          Center(
            child: _Doll(
              mood: mood,
              talking: talking,
              delivering: chat.cardJustArrived,
            ),
          ),
          // 情绪小标签
          Positioned(
            left: 16,
            top: 16,
            child: Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
              decoration: BoxDecoration(
                color: Colors.black.withValues(alpha: 0.18),
                borderRadius: BorderRadius.circular(20),
                border: Border.all(color: mood.tint.withValues(alpha: 0.25)),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  Container(
                    width: 5,
                    height: 5,
                    decoration: BoxDecoration(
                      color: mood.tint,
                      shape: BoxShape.circle,
                    ),
                  ),
                  const SizedBox(width: 6),
                  Text(
                    mood.label,
                    style: TextStyle(
                      fontSize: 10.5,
                      color: mood.tint.withValues(alpha: 0.95),
                      letterSpacing: 0.5,
                    ),
                  ),
                ],
              ),
            ),
          ),
          // 右下：在说话时给一条声波
          Positioned(
            right: 18,
            bottom: 18,
            child: AnimatedOpacity(
              opacity: talking ? 1 : 0,
              duration: const Duration(milliseconds: 320),
              child: _Waveform(color: mood.tint, height: 16),
            ),
          ),
          Positioned(
            left: 18,
            bottom: 17,
            child: Text(
              talking ? '正在回应你' : '陪着你',
              style: TextStyle(
                fontSize: 10,
                letterSpacing: 1.2,
                color: Colors.white.withValues(alpha: 0.32),
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ---------------- 消息 ----------------

  Widget _userLine(String text) {
    return Align(
      alignment: Alignment.centerRight,
      child: Container(
        margin: const EdgeInsets.only(top: 12, bottom: 6, left: 44),
        padding: const EdgeInsets.symmetric(horizontal: 15, vertical: 10),
        decoration: BoxDecoration(
          color: _accent.withValues(alpha: 0.13),
          borderRadius: const BorderRadius.only(
            topLeft: Radius.circular(18),
            topRight: Radius.circular(18),
            bottomLeft: Radius.circular(18),
            bottomRight: Radius.circular(6),
          ),
          border: Border.all(color: _accent.withValues(alpha: 0.16)),
        ),
        child: Text(
          text,
          style: TextStyle(
            fontSize: 13.5,
            height: 1.55,
            color: _textPrimary.withValues(alpha: 0.94),
          ),
        ),
      ),
    );
  }

  Widget _assistantLine(ChatProvider chat, ChatMessage msg, bool thinking) {
    final bool speaking = identical(chat.speakingMessage, msg);
    return Padding(
      padding: const EdgeInsets.only(top: 6, bottom: 14, right: 20),
      child: Container(
        padding: const EdgeInsets.only(left: 13),
        child: Stack(
          children: <Widget>[
            Positioned(
              left: 0,
              top: 4,
              bottom: 4,
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 280),
                width: 2,
                decoration: BoxDecoration(
                  color: speaking
                      ? _accent.withValues(alpha: 0.85)
                      : _accent.withValues(alpha: 0.0),
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            ),
            if (thinking)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 8),
                child: _ThinkingDots(),
              )
            else
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text(
                    msg.text,
                    style: const TextStyle(
                      fontSize: 15.2,
                      height: 1.85,
                      letterSpacing: 0.2,
                      color: _textPrimary,
                    ),
                  ),
                  // 语音没起来时的一行灰字：紧跟正文下方、左对齐，无卡片无边框；
                  // 只挂在本轮最新一条助手消息上，合成恢复后由 provider 自动清空。
                  if (chat.ttsNotice != null &&
                      identical(msg, chat.activeAssistant))
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: Text(
                        chat.ttsNotice!,
                        style: const TextStyle(
                          fontSize: 12,
                          height: 1.5,
                          letterSpacing: 0.2,
                          color: _textMuted,
                        ),
                      ),
                    ),
                  if (msg.card != null) _deliverCard(chat, msg),
                ],
              ),
          ],
        ),
      ),
    );
  }

  /// 递到手上的那张纸：标题定调，条目能照着做，底下一句「这张对吗」。
  ///
  /// 为什么要有卡：以前整条链路的出口只有一句话，说完就结束了，
  /// 听完还得自己从一大段里再摘一遍。卡是第二个出口——把干货压成一张纸递过去。
  Widget _deliverCard(ChatProvider chat, ChatMessage msg) {
    final DeliveryCard card = msg.card!;
    final bool rated = msg.feedback != 0;
    return Padding(
      padding: const EdgeInsets.only(top: 12, right: 2),
      child: Container(
        padding: const EdgeInsets.fromLTRB(15, 13, 15, 9),
        decoration: BoxDecoration(
          color: _surface.withValues(alpha: 0.55),
          borderRadius: BorderRadius.circular(15),
          border: Border.all(color: _accent.withValues(alpha: 0.14)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Container(
                  width: 3,
                  height: 15,
                  decoration: BoxDecoration(
                    color: _accent.withValues(alpha: 0.75),
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
                const SizedBox(width: 9),
                Expanded(
                  child: Text(
                    card.title,
                    style: TextStyle(
                      fontFamily: Type.serif,
                      fontSize: 15.5,
                      height: 1.5,
                      letterSpacing: 0.3,
                      color: _textPrimary,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 9),
            for (int i = 0; i < card.items.length; i++)
              Padding(
                padding: const EdgeInsets.only(bottom: 7),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    SizedBox(
                      width: 15,
                      child: Text(
                        '${i + 1}',
                        style: TextStyle(
                          fontSize: 12,
                          height: 1.6,
                          color: _accent.withValues(alpha: 0.8),
                        ),
                      ),
                    ),
                    Expanded(
                      child: Text(
                        card.items[i],
                        style: TextStyle(
                          fontSize: 13.5,
                          height: 1.65,
                          color: _textPrimary.withValues(alpha: 0.92),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            const SizedBox(height: 2),
            Row(
              children: <Widget>[
                Expanded(
                  child: Text(
                    rated
                        ? (msg.feedback > 0 ? '已记下：这张对' : '已记下：这张不对')
                        : (card.note.isEmpty ? '这张对吗？' : card.note),
                    style: TextStyle(
                      fontSize: 10.5,
                      height: 1.4,
                      color: _textMuted.withValues(alpha: 0.85),
                    ),
                  ),
                ),
                _ratePill(chat, msg, true),
                const SizedBox(width: 6),
                _ratePill(chat, msg, false),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _ratePill(ChatProvider chat, ChatMessage msg, bool good) {
    final bool on = msg.feedback == (good ? 1 : -1);
    final Color tint = good ? Palette.olive : _danger;
    return GestureDetector(
      onTap: () {
        HapticFeedback.selectionClick();
        unawaited(chat.rateMessage(msg, good));
      },
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 5),
        decoration: BoxDecoration(
          color: on ? tint.withValues(alpha: 0.16) : Colors.transparent,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
            color: on
                ? tint.withValues(alpha: 0.42)
                : Colors.white.withValues(alpha: 0.12),
          ),
        ),
        child: Text(
          good ? '对了' : '不对',
          style: TextStyle(
            fontSize: 10.5,
            letterSpacing: 0.4,
            color: on ? tint : _textMuted,
          ),
        ),
      ),
    );
  }

  Widget _emptyHint() {
    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Center(
        child: Text(
          '先从一句「在吗」开始吧',
          style: TextStyle(
            fontSize: 12,
            letterSpacing: 1,
            color: _textMuted.withValues(alpha: 0.75),
          ),
        ),
      ),
    );
  }

  // ---------------- 底部 ----------------

  Widget _speakingBar(ChatProvider chat) {
    return Align(
      alignment: Alignment.centerLeft,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(22, 0, 22, 8),
        child: GestureDetector(
          onTap: () => unawaited(chat.stopSpeaking()),
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 9),
            decoration: BoxDecoration(
              color: _accent.withValues(alpha: 0.09),
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: _accent.withValues(alpha: 0.22)),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                const _Waveform(color: _accent, height: 11),
                const SizedBox(width: 9),
                const Text(
                  '正在说…',
                  style: TextStyle(fontSize: 11, color: _accent),
                ),
                const SizedBox(width: 10),
                Container(
                  width: 1,
                  height: 10,
                  color: _accent.withValues(alpha: 0.25),
                ),
                const SizedBox(width: 10),
                Text(
                  '停下',
                  style: TextStyle(
                    fontSize: 11,
                    color: _accent.withValues(alpha: 0.65),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _errorBar(ChatProvider chat) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(22, 0, 22, 8),
      child: Row(
        children: <Widget>[
          const Icon(Icons.error_outline_rounded, size: 13, color: _danger),
          const SizedBox(width: 7),
          Expanded(
            child: Text(
              chat.lastError ?? '',
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 11, color: _danger),
            ),
          ),
          GestureDetector(
            onTap: chat.dismissError,
            child: Padding(
              padding: const EdgeInsets.only(left: 8),
              child: Icon(Icons.close_rounded,
                  size: 13, color: _danger.withValues(alpha: 0.6)),
            ),
          ),
        ],
      ),
    );
  }

  Widget _composer(ChatProvider chat) {
    final bool recording = chat.isRecording;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 220),
        height: 54,
        padding: const EdgeInsets.symmetric(horizontal: 6),
        decoration: BoxDecoration(
          color: recording
              ? _danger.withValues(alpha: 0.14)
              : _surface.withValues(alpha: 0.92),
          borderRadius: BorderRadius.circular(27),
          border: Border.all(
            color: recording
                ? _danger.withValues(alpha: 0.45)
                : Colors.white.withValues(alpha: 0.06),
          ),
          boxShadow: <BoxShadow>[
            BoxShadow(
              color: Colors.black.withValues(alpha: 0.40),
              blurRadius: 26,
              offset: const Offset(0, 12),
            ),
          ],
        ),
        child: Row(
          children: <Widget>[
            _micButton(chat, recording),
            const SizedBox(width: 4),
            Expanded(
              child: recording
                  ? Row(
                      children: <Widget>[
                        const _Waveform(color: _danger, height: 13),
                        const SizedBox(width: 10),
                        Text(
                          '松手发送',
                          style: TextStyle(
                            fontSize: 12.5,
                            color: _danger.withValues(alpha: 0.9),
                          ),
                        ),
                      ],
                    )
                  : TextField(
                      controller: _input,
                      autocorrect: false,
                      maxLines: 1,
                      textInputAction: TextInputAction.send,
                      onSubmitted: (_) => _submit(),
                      style: const TextStyle(
                        fontSize: 14,
                        color: _textPrimary,
                      ),
                      cursorColor: _accent,
                      cursorWidth: 1.4,
                      decoration: InputDecoration(
                        isDense: true,
                        border: InputBorder.none,
                        hintText: chat.isRecognizing ? '正在听懂…' : '说点什么…',
                        hintStyle: TextStyle(
                          fontSize: 13,
                          color: _textMuted.withValues(alpha: 0.8),
                        ),
                      ),
                    ),
            ),
            ValueListenableBuilder<TextEditingValue>(
              valueListenable: _input,
              builder: (BuildContext context, TextEditingValue value, _) {
                if (chat.isStreaming) {
                  return _RoundButton(
                    icon: Icons.stop_rounded,
                    background: _textMuted.withValues(alpha: 0.16),
                    foreground: _textPrimary.withValues(alpha: 0.75),
                    onTap: () {
                      HapticFeedback.selectionClick();
                      chat.cancel();
                    },
                  );
                }
                final bool ready = value.text.trim().isNotEmpty;
                return _RoundButton(
                  icon: Icons.arrow_upward_rounded,
                  background: ready
                      ? _accent
                      : Colors.white.withValues(alpha: 0.06),
                  foreground:
                      ready ? const Color(0xFF241A10) : _textMuted,
                  onTap: ready ? _submit : null,
                );
              },
            ),
          ],
        ),
      ),
    );
  }

  Widget _micButton(ChatProvider chat, bool recording) {
    return Listener(
      onPointerDown: (_) {
        if (chat.isStreaming) return;
        HapticFeedback.mediumImpact();
        unawaited(chat.startRecording());
      },
      onPointerUp: (_) {
        if (chat.isRecording) unawaited(chat.stopRecordingAndRecognize());
      },
      onPointerCancel: (_) {
        if (chat.isRecording) unawaited(chat.stopRecordingAndRecognize());
      },
      child: Container(
        width: 42,
        height: 42,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: recording
              ? _danger
              : Colors.white.withValues(alpha: 0.05),
          boxShadow: recording
              ? <BoxShadow>[
                  BoxShadow(
                    color: _danger.withValues(alpha: 0.45),
                    blurRadius: 18,
                  ),
                ]
              : null,
        ),
        child: Icon(
          chat.isRecognizing ? Icons.graphic_eq_rounded : Icons.mic_rounded,
          size: 19,
          color: recording
              ? Colors.white
              : _textMuted.withValues(alpha: 0.95),
        ),
      ),
    );
  }

  // ---------------- 菜单 ----------------

  Future<void> _openServerDialog(BuildContext context) async {
    final TextEditingController controller =
        TextEditingController(text: widget.chat.serverUrl);
    String? errorText;
    final bool? saved = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) {
        return StatefulBuilder(
          builder: (BuildContext ctx, StateSetter setState) {
            return AlertDialog(
              backgroundColor: _surface,
              title: const Text('服务器地址', style: TextStyle(fontSize: 16)),
              content: Column(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  TextField(
                    controller: controller,
                    autocorrect: false,
                    keyboardType: TextInputType.url,
                    style: const TextStyle(fontSize: 13),
                    decoration: InputDecoration(
                      labelText: '大脑服务地址',
                      hintText: 'http://192.168.1.5:8000',
                      errorText: errorText,
                      isDense: true,
                    ),
                  ),
                  const SizedBox(height: 10),
                  const Text(
                    '模拟器用 10.0.2.2；真机填电脑局域网 IP，需同一 WiFi。',
                    style: TextStyle(fontSize: 11, color: _textMuted),
                  ),
                ],
              ),
              actions: <Widget>[
                TextButton(
                  onPressed: () => Navigator.of(ctx).pop(false),
                  child: const Text('取消', style: TextStyle(fontSize: 13)),
                ),
                TextButton(
                  onPressed: () async {
                    try {
                      await widget.chat.setServerUrl(controller.text);
                      if (ctx.mounted) Navigator.of(ctx).pop(true);
                    } catch (error) {
                      final String message = error is ArgumentError
                          ? (error.message ?? '地址不合法').toString()
                          : error.toString();
                      setState(() => errorText = message);
                    }
                  },
                  child: const Text('保存', style: TextStyle(fontSize: 13)),
                ),
              ],
            );
          },
        );
      },
    );
    controller.dispose();
    if (saved == true && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('已切换到 ${widget.chat.serverUrl}',
              style: const TextStyle(fontSize: 12)),
          duration: const Duration(seconds: 2),
        ),
      );
      // 立刻探一次，连上了提示条就会自己消失。
      unawaited(_checkUpdate());
    }
  }

  Future<void> _openMenu(BuildContext context) async {
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: _surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (BuildContext ctx) {
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              ListTile(
                dense: true,
                leading: const Icon(Icons.refresh_rounded,
                    size: 18, color: _textMuted),
                title: const Text('检查更新',
                    style: TextStyle(fontSize: 13, color: _textPrimary)),
                onTap: () {
                  Navigator.of(ctx).pop();
                  unawaited(_manualCheck());
                },
              ),
              ListTile(
                dense: true,
                leading: const Icon(Icons.dns_outlined,
                    size: 18, color: _textMuted),
                title: const Text('服务器地址',
                    style: TextStyle(fontSize: 13, color: _textPrimary)),
                subtitle: Text(widget.chat.serverUrl,
                    style:
                        const TextStyle(fontSize: 10.5, color: _textMuted)),
                onTap: () {
                  Navigator.of(ctx).pop();
                  unawaited(_openServerDialog(context));
                },
              ),
              ListTile(
                dense: true,
                leading: const Icon(Icons.delete_outline_rounded,
                    size: 18, color: _textMuted),
                title: const Text('清空对话',
                    style: TextStyle(fontSize: 13, color: _textPrimary)),
                onTap: () {
                  Navigator.of(ctx).pop();
                  widget.chat.clear();
                },
              ),
            ],
          ),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    final ChatProvider chat = widget.chat;
    return Scaffold(
      body: DecoratedBox(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: <Color>[_bgTop, _bgBottom],
            stops: <double>[0.0, 0.9],
          ),
        ),
        child: SafeArea(
          bottom: false,
          child: ListenableBuilder(
            listenable: chat,
            builder: (BuildContext context, Widget? _) {
              _follow();
              final List<ChatMessage> list = chat.messages;
              final int n = list.length;
              return Column(
                children: <Widget>[
                  _topBar(chat),
                  if (_offline) _offlineBanner(),
                  if (!_offline && _update != null) _updateBanner(_update!),
                  Expanded(
                    child: ListView.builder(
                      controller: _scroll,
                      padding: const EdgeInsets.fromLTRB(22, 2, 22, 16),
                      itemCount: 1 + (n == 0 ? 1 : n),
                      itemBuilder: (BuildContext context, int i) {
                        if (i == 0) return _presenceCard(chat);
                        if (n == 0) return _emptyHint();
                        final ChatMessage msg = list[i - 1];
                        if (msg.role == 'user') return _userLine(msg.text);
                        final bool thinking = i == n &&
                            msg.text.isEmpty &&
                            (chat.isStreaming || chat.isSpeaking);
                        return _assistantLine(chat, msg, thinking);
                      },
                    ),
                  ),
                  if (chat.isSpeaking) _speakingBar(chat),
                  if (chat.lastError != null) _errorBar(chat),
                  _composer(chat),
                ],
              );
            },
          ),
        ),
      ),
    );
  }
}

// ---------------- 小部件 ----------------

/// 顶部状态小圆点：有活动时缓慢呼吸。
class _LiveDot extends StatefulWidget {
  const _LiveDot({required this.active});

  final bool active;

  @override
  State<_LiveDot> createState() => _LiveDotState();
}

class _LiveDotState extends State<_LiveDot>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1800),
  )..repeat(reverse: true);

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _c,
      builder: (BuildContext context, _) {
        final double t = widget.active ? _c.value : 0.35;
        return Container(
          width: 6,
          height: 6,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: _accent.withValues(alpha: 0.35 + 0.55 * t),
            boxShadow: <BoxShadow>[
              BoxShadow(
                color: _accent.withValues(alpha: 0.35 * t),
                blurRadius: 8,
              ),
            ],
          ),
        );
      },
    );
  }
}

/// 几条上下跳动的声波柱。
class _Waveform extends StatefulWidget {
  const _Waveform({required this.color, required this.height});

  final Color color;
  final double height;
  final int bars = 4;

  @override
  State<_Waveform> createState() => _WaveformState();
}

class _WaveformState extends State<_Waveform>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
  )..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _c,
      builder: (BuildContext context, _) {
        return Row(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.center,
          children: List<Widget>.generate(widget.bars, (int i) {
            final double phase = _c.value * 2 * math.pi + i * 1.1;
            final double scale = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(phase));
            return Padding(
              padding: const EdgeInsets.symmetric(horizontal: 1.5),
              child: Container(
                width: 2.4,
                height: widget.height * scale,
                decoration: BoxDecoration(
                  color: widget.color.withValues(alpha: 0.85),
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            );
          }),
        );
      },
    );
  }
}

/// 「正在想…」的三个点。
class _ThinkingDots extends StatefulWidget {
  const _ThinkingDots();

  @override
  State<_ThinkingDots> createState() => _ThinkingDotsState();
}

class _ThinkingDotsState extends State<_ThinkingDots>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1200),
  )..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _c,
      builder: (BuildContext context, _) {
        return Row(
          mainAxisSize: MainAxisSize.min,
          children: List<Widget>.generate(3, (int i) {
            final double t = (_c.value - i * 0.16) % 1.0;
            final double lift =
                t < 0.5 ? math.sin(t * 2 * math.pi) * 1.0 : 0.0;
            return Padding(
              padding: const EdgeInsets.only(right: 5),
              child: Transform.translate(
                offset: Offset(0, -3 * math.sin(t * math.pi)),
                child: Container(
                  width: 5,
                  height: 5,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: _accent.withValues(alpha: 0.35 + 0.4 * lift),
                  ),
                ),
              ),
            );
          }),
        );
      },
    );
  }
}

class _RoundButton extends StatelessWidget {
  const _RoundButton({
    required this.icon,
    required this.background,
    required this.foreground,
    this.onTap,
  });

  final IconData icon;
  final Color background;
  final Color foreground;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        width: 42,
        height: 42,
        decoration: BoxDecoration(shape: BoxShape.circle, color: background),
        child: Icon(icon, size: 19, color: foreground),
      ),
    );
  }
}

class _IconTap extends StatelessWidget {
  const _IconTap({required this.icon, required this.onTap});

  final IconData icon;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        width: 38,
        height: 38,
        alignment: Alignment.center,
        child: Icon(icon, size: 20, color: _textMuted),
      ),
    );
  }
}

// ---------------- 角色：一滴墨 ----------------

/// 非人形角色。没有五官、没有四肢，所以不存在「像不像人」的问题。
/// 呼吸=它在；说话起涟漪=它在说；分裂出薄片=它把东西递到你手上。
class _Doll extends StatelessWidget {
  const _Doll({required this.mood, required this.talking, this.delivering = false});

  final _Mood mood;
  final bool talking;
  final bool delivering;

  @override
  Widget build(BuildContext context) {
    return InkDrop(
      tint: mood.tint,
      smile: mood.smile,
      talking: talking,
      delivering: delivering,
      size: 150,
    );
  }
}
