// 临时验证脚本（T-03）：
// 用例 1：首句 TTS 失败 → 出现可读提示；后续合成成功 → 提示自动消失。
// 用例 2：先合成成功、中途一句失败 → 不新增/不叠加提示（语音已经起来过）。
// 本地 HttpServer 模拟大脑服务：/chat 推两句 SSE，/tts 按用例配置返回 503 / 200。
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:companion_client/chat_provider.dart';
import 'package:companion_client/config.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  // 测试绑定默认把 HttpClient 伪造成 400；这里恢复 dart:io 真实客户端，
  // 才能打到本地 HttpServer 上的假后端。
  HttpOverrides.global = null;

  late HttpServer server;
  late ChatProvider provider;
  int ttsCalls = 0;
  // failFirst：首句 503、后面 200；failAfter：首句 200、后面 503。
  String mode = 'failFirst';

  setUp(() async {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    server = await HttpServer.bind('127.0.0.1', 0);
    ttsCalls = 0;
    server.listen((HttpRequest req) async {
      final String path = req.uri.path;
      if (path == '/chat') {
        req.response.headers.contentType = ContentType(
          'text', 'event-stream', charset: 'utf-8',
        );
        final HttpResponse sink = req.response;
        void send(String json) =>
            sink.add(utf8.encode('data: $json\n\n'));
        send(jsonEncode(<String, dynamic>{
          'type': 'token',
          'delta': '第一句话内容足够长。',
        }));
        send(jsonEncode(<String, dynamic>{
          'type': 'token',
          'delta': '第二句话内容也足够长。',
        }));
        send(jsonEncode(<String, dynamic>{
          'type': 'done',
          'emotion': 'neutral',
        }));
        await sink.close();
      } else if (path == '/tts') {
        ttsCalls++;
        final bool fail = mode == 'failFirst'
            ? ttsCalls <= 2
            : ttsCalls >= 2;
        if (fail) {
          req.response.statusCode = 503;
          req.response.headers.contentType =
              ContentType('application', 'json', charset: 'utf-8');
          req.response.add(utf8.encode(jsonEncode(<String, dynamic>{
            'detail': '连不上 GPT-SoVITS（9880 拒绝连接）',
            'code': 'TTS_UNAVAILABLE',
          })));
        } else {
          // 成功一句稍慢一点，留出采样「提示还在」的窗口。
          await Future<void>.delayed(const Duration(milliseconds: 500));
          req.response.statusCode = 200;
          req.response.headers.contentType = ContentType('audio', 'wav');
          req.response.add(List<int>.filled(44, 0));
        }
        await req.response.close();
      } else {
        req.response.statusCode = 404;
        await req.response.close();
      }
    });
    final AppConfig config = AppConfig();
    await config.setBrainUrl('http://127.0.0.1:${server.port}');
    provider = ChatProvider(config);
  });

  tearDown(() async {
    provider.dispose();
    await server.close();
  });

  Future<void> waitUntil(bool Function() condition,
      [int ticks = 60]) async {
    for (int i = 0; i < ticks; i++) {
      if (condition()) return;
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }
  }

  test('首句失败出现提示，后续成功后提示自动消失', () async {
    mode = 'failFirst';
    unawaited(provider.send('在吗'));

    // 1) 等首句两次重试都失败、提示挂上。
    await waitUntil(() => provider.ttsNotice != null);
    final String? notice = provider.ttsNotice;
    expect(notice, isNotNull, reason: '首句两次失败后应出现一行可读提示');
    expect(notice!.startsWith('语音没起来：'), isTrue);
    expect(notice, contains('连不上 GPT-SoVITS'));
    expect(notice, contains('TTS_UNAVAILABLE'));
    // 同轮不叠加：提示仍是一行。
    expect(notice.split('\n').length, 1);

    // 2) 等第二句合成成功，提示应被清空。
    await waitUntil(() => provider.ttsNotice == null);
    expect(provider.ttsNotice, isNull,
        reason: '后续合成成功一次后提示应自动消失');
    expect(ttsCalls, greaterThanOrEqualTo(3));
  });

  test('先成功、中途一句失败：不新增提示', () async {
    mode = 'failAfter';
    unawaited(provider.send('在吗'));

    // 等两次重试都打完（成功 1 次 + 失败句重试 2 次）。
    await waitUntil(() => ttsCalls >= 3);
    await Future<void>.delayed(const Duration(milliseconds: 300));
    expect(provider.ttsNotice, isNull,
        reason: '本轮已经出声过，中途失败不应再挂提示');
    expect(ttsCalls, greaterThanOrEqualTo(3));
  });
}
