import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'chat_provider.dart';
import 'config.dart';
import 'system_shell.dart';

// ---- 配色：深夜暖木。底色压到近黑，只有形象区和强调色带一点暖光 ----
const Color _bgTop = Color(0xFF17130F);
const Color _bgBottom = Color(0xFF0C0A09);
const Color _surface = Color(0xFF221C17);
const Color _textPrimary = Color(0xFFF3EBE1);
const Color _textMuted = Color(0xFF94897D);
const Color _accent = Color(0xFFE9A96A);
const Color _danger = Color(0xFFE0705F);

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
        brightness: Brightness.dark,
        colorScheme: const ColorScheme.dark(
          primary: _accent,
          surface: _surface,
          error: _danger,
        ),
        scaffoldBackgroundColor: _bgTop,
        splashFactory: NoSplash.splashFactory,
        fontFamilyFallback: const <String>['Noto Sans CJK SC', 'sans-serif'],
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
          Center(child: _Doll(mood: mood, talking: talking)),
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
              Text(
                msg.text,
                style: const TextStyle(
                  fontSize: 15.2,
                  height: 1.85,
                  letterSpacing: 0.2,
                  color: _textPrimary,
                ),
              ),
          ],
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
  const _Waveform({required this.color, required this.height, this.bars = 4});

  final Color color;
  final double height;
  final int bars;

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

// ---------------- 玩偶 ----------------

/// 会呼吸、会眨眼、说话时嘴巴一张一合的小玩偶。
class _Doll extends StatefulWidget {
  const _Doll({required this.mood, required this.talking});

  final _Mood mood;
  final bool talking;

  @override
  State<_Doll> createState() => _DollState();
}

class _DollState extends State<_Doll> with TickerProviderStateMixin {
  /// 呼吸：慢，控制整体起伏和光晕。
  late final AnimationController _breath = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 4200),
  )..repeat();

  /// 眨眼：4.6 秒一轮，其中最后一小段完成一次闭合。
  late final AnimationController _blink = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 4600),
  )..repeat();

  /// 说话：嘴巴开合的频率。
  late final AnimationController _mouth = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 900),
  );

  @override
  void initState() {
    super.initState();
    if (widget.talking) _mouth.repeat();
  }

  @override
  void didUpdateWidget(covariant _Doll oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.talking && !_mouth.isAnimating) {
      _mouth.repeat();
    } else if (!widget.talking && _mouth.isAnimating) {
      _mouth.stop();
    }
  }

  @override
  void dispose() {
    _breath.dispose();
    _blink.dispose();
    _mouth.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 158,
      height: 158,
      child: AnimatedBuilder(
        animation: Listenable.merge(
          <Listenable>[_breath, _blink, _mouth],
        ),
        builder: (BuildContext context, _) {
          // 眨眼的闭合量：大部分时间是 0，接近循环末尾迅速闭合再张开。
          final double b = _blink.value;
          final double blink = b > 0.90
              ? (1 - (b - 0.90) / 0.10).clamp(0.0, 1.0)
              : 0.0;
          final double mouth = widget.talking
              ? 0.5 + 0.5 * math.sin(_mouth.value * 2 * math.pi)
              : 0.0;
          return CustomPaint(
            painter: _DollPainter(
              mood: widget.mood,
              breath: _breath.value,
              blink: 1 - blink,
              mouth: mouth,
              talking: widget.talking,
            ),
          );
        },
      ),
    );
  }
}

class _DollPainter extends CustomPainter {
  _DollPainter({
    required this.mood,
    required this.breath,
    required this.blink,
    required this.mouth,
    required this.talking,
  });

  final _Mood mood;

  /// 0～1 循环，用来做呼吸起伏。
  final double breath;

  /// 眼皮闭合比例：1 = 完全睁开，0 = 闭上。
  final double blink;

  /// 嘴巴张开量 0～1。
  final double mouth;

  final bool talking;

  @override
  void paint(Canvas canvas, Size size) {
    final double w = size.width;
    final double h = size.height;
    final double cx = w / 2;
    final double bob = math.sin(breath * 2 * math.pi) * 2.2;
    canvas.save();
    canvas.translate(0, bob);

    // 地面影子
    final Paint shadow = Paint()
      ..color = Colors.black.withValues(alpha: 0.30)
      ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 9);
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx, h * 0.90),
        width: w * 0.46,
        height: h * 0.075,
      ),
      shadow,
    );

    // 说话时外圈有一道缓慢转动的光弧
    if (talking) {
      final Paint halo = Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.6
        ..strokeCap = StrokeCap.round
        ..color = mood.tint.withValues(alpha: 0.35);
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(cx, h * 0.48),
          width: w * 0.84,
          height: h * 0.86,
        ),
        breath * 2 * math.pi,
        1.5,
        false,
        halo,
      );
    }

    // 身体：一颗竖着的圆润小石
    final Rect bodyRect = Rect.fromCenter(
      center: Offset(cx, h * 0.50),
      width: w * 0.60,
      height: h * 0.74,
    );
    final RRect body = RRect.fromRectAndRadius(
      bodyRect,
      Radius.circular(w * 0.30),
    );

    canvas.drawRRect(
      body,
      Paint()
        ..shader = LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: <Color>[
            Color.lerp(mood.tint, Colors.white, 0.42)!,
            Color.lerp(mood.tint, Colors.black, 0.18)!,
          ],
        ).createShader(bodyRect),
    );

    // 顶部高光
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx - w * 0.10, h * 0.28),
        width: w * 0.20,
        height: h * 0.09,
      ),
      Paint()
        ..color = Colors.white.withValues(alpha: 0.30)
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 7),
    );

    final double eyeY = h * 0.44;
    final double eyeDx = w * 0.115;
    final double eyeW = w * 0.105;
    final double eyeH = h * 0.085;

    // 腮红
    final Paint blush = Paint()
      ..color = const Color(0xFFE0705F).withValues(alpha: 0.20)
      ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 6);
    canvas.drawCircle(Offset(cx - w * 0.20, h * 0.545), w * 0.055, blush);
    canvas.drawCircle(Offset(cx + w * 0.20, h * 0.545), w * 0.055, blush);

    for (final double dir in <double>[-1, 1]) {
      final Offset center = Offset(cx + dir * eyeDx, eyeY);

      // 眼白
      canvas.drawOval(
        Rect.fromCenter(center: center, width: eyeW, height: eyeH),
        Paint()..color = const Color(0xFFFFFBF5).withValues(alpha: 0.96),
      );
      // 眼珠
      canvas.drawCircle(
        center.translate(0, eyeH * 0.05),
        eyeW * 0.30,
        Paint()..color = const Color(0xFF3B2C22),
      );
      // 高光
      canvas.drawCircle(
        center.translate(-eyeW * 0.10, -eyeH * 0.10),
        eyeW * 0.10,
        Paint()..color = Colors.white.withValues(alpha: 0.9),
      );

      // 眨眼：用身体色从上往下盖住眼睛
      if (blink < 0.99) {
        final double lidH = eyeH * (1 - blink) * 1.05;
        canvas.save();
        canvas.clipRRect(body);
        canvas.drawRect(
          Rect.fromLTWH(
            center.dx - eyeW * 0.62,
            center.dy - eyeH * 0.62,
            eyeW * 1.24,
            lidH,
          ),
          Paint()
            ..color = Color.lerp(mood.tint, Colors.white, 0.30)!,
        );
        canvas.restore();
        // 闭眼时画一条线，更像眼皮
        if (blink < 0.12) {
          canvas.drawLine(
            Offset(center.dx - eyeW * 0.32, center.dy),
            Offset(center.dx + eyeW * 0.32, center.dy),
            Paint()
              ..color = const Color(0xFF4A382C).withValues(alpha: 0.7)
              ..strokeWidth = 1.6
              ..strokeCap = StrokeCap.round,
          );
        }
      }

      // 眉毛：开心时外高内低，难过 / 生气时反过来
      final double tilt = -mood.smile * 0.22;
      final Offset inner = Offset(
        center.dx - dir * eyeW * 0.46,
        center.dy - eyeH * 0.95 + tilt * eyeH,
      );
      final Offset outer = Offset(
        center.dx + dir * eyeW * 0.46,
        center.dy - eyeH * 0.95 - tilt * eyeH,
      );
      canvas.drawLine(
        inner,
        outer,
        Paint()
          ..color = const Color(0xFF5B4334).withValues(alpha: 0.55)
          ..strokeWidth = 2.0
          ..strokeCap = StrokeCap.round,
      );
    }

    // 嘴巴
    final Offset mouthCenter = Offset(cx, h * 0.605);
    final Paint mouthPaint = Paint()
      ..color = const Color(0xFF5B3A2C).withValues(alpha: 0.85)
      ..style = PaintingStyle.fill;

    if (talking) {
      final double openH = h * (0.012 + 0.055 * mouth);
      canvas.drawOval(
        Rect.fromCenter(
          center: mouthCenter,
          width: w * 0.10,
          height: openH,
        ),
        mouthPaint,
      );
    } else {
      final Path smile = Path()..moveTo(cx - w * 0.055, mouthCenter.dy);
      final double depth = h * 0.030 * mood.smile;
      smile.quadraticBezierTo(
        cx,
        mouthCenter.dy + depth,
        cx + w * 0.055,
        mouthCenter.dy,
      );
      canvas.drawPath(
        smile,
        Paint()
          ..color = const Color(0xFF5B3A2C).withValues(alpha: 0.8)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2.0
          ..strokeCap = StrokeCap.round,
      );
    }

    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant _DollPainter old) {
    return old.breath != breath ||
        old.blink != blink ||
        old.mouth != mouth ||
        old.talking != talking ||
        old.mood != mood;
  }
}
