import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

/// 一帧 SSE 事件。后端每帧是一条 `data:` 行，内容为 JSON 对象。
class SseEvent {
  SseEvent(this.data);

  /// 原始 JSON 对象。
  final Map<String, dynamic> data;

  String? _str(String key) {
    final Object? value = data[key];
    return value is String ? value : null;
  }

  String get type => _str('type') ?? '';
  String? get delta => _str('delta');
  String? get question => _str('question');
  String? get emotion => _str('emotion');
  String? get message => _str('message');
  String? get code => _str('code');
  bool get retryable => data['retryable'] == true;
  double? get intensity =>
      data['intensity'] is num ? (data['intensity'] as num).toDouble() : null;

  // ---- card 帧（后端递出来的那张纸）----
  String? get requestId => _str('request_id');
  String? get title => _str('title');
  String? get kind => _str('kind');
  String? get note => _str('note');
  int? get deliveryId => data['delivery_id'] is num
      ? (data['delivery_id'] as num).toInt()
      : null;
  List<String> get items {
    final Object? value = data['items'];
    if (value is List) {
      return value.map((Object? e) => '${e ?? ''}'.trim()).toList(growable: false);
    }
    return const <String>[];
  }
}

/// 用 package:http 的 `Client.send()` 直连大脑服务（不使用 dio）。
///
/// SSE 必须禁 gzip：`Accept-Encoding: identity`，否则中间缓冲会把帧粘住。
class SseClient {
  SseClient(this.baseUrl);

  /// 形如 `http://10.0.2.2:8000`，不带末尾斜杠。
  String baseUrl;

  /// POST /chat，逐帧吐出 [SseEvent]。
  ///
  /// 收帧策略：后端每帧是一条 `data:` 行 + 帧间空行。LineSplitter 可能吃掉
  /// 空行，因此不单独依赖空行，而是用「遇到下一条 data: 行 / 注释行 / 空行 /
  /// 流结束」来收帧。`utf8.decoder` 是增量解码，跨 chunk 的中文不会被切断。
  Stream<SseEvent> chat({
    required String text,
    String userId = 'u1',
    String sessionId = 's1',
    String? requestId,
  }) async* {
    final http.Client client = http.Client();
    try {
      final http.Request request = http.Request('POST', Uri.parse('$baseUrl/chat'));
      request.headers.addAll(<String, String>{
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        // SSE 禁 gzip：identity 保证字节原样送达，不被压缩/缓冲。
        'Accept-Encoding': 'identity',
        'Cache-Control': 'no-cache',
      });
      // 用 bodyBytes 而非 body setter，避免 http 自动补一个 content-type 头。
      request.bodyBytes = utf8.encode(jsonEncode(<String, dynamic>{
        'user_id': userId,
        'session_id': sessionId,
        'text': text,
        if (requestId != null) 'request_id': requestId,
      }));

      final http.StreamedResponse response = await client.send(request);
      if (response.statusCode != 200) {
        final String body =
            await response.stream.transform(utf8.decoder).join();
        yield SseEvent(<String, dynamic>{
          'type': 'error',
          'code': 'HTTP_${response.statusCode}',
          'message': '服务返回 HTTP ${response.statusCode}：$body',
          'retryable': true,
        });
        return;
      }

      final Stream<String> lines = response.stream
          .transform(utf8.decoder)
          .transform(const LineSplitter());

      final List<String> frameLines = <String>[];
      await for (final String line in lines) {
        if (line.startsWith(':')) {
          // 注释帧（: ping 心跳）：先收上一帧，然后跳过本行。
          final SseEvent? pending = _takeFrame(frameLines);
          if (pending != null) yield pending;
          continue;
        }
        if (line.isEmpty) {
          // 帧间空行（若 LineSplitter 保留了它）也收帧。
          final SseEvent? pending = _takeFrame(frameLines);
          if (pending != null) yield pending;
          continue;
        }
        if (line.startsWith('data:')) {
          // 遇到下一条 data: → 先收上一帧（应对空行被吃掉的情况）。
          final SseEvent? pending = _takeFrame(frameLines);
          if (pending != null) yield pending;

          String value = line.substring('data:'.length);
          if (value.startsWith(' ')) value = value.substring(1);
          frameLines.add(value);
          continue;
        }
        // event: / id: / retry: 等其它 SSE 字段本期忽略。
      }
      // 流结束 → 收最后一帧。
      final SseEvent? last = _takeFrame(frameLines);
      if (last != null) yield last;
    } finally {
      // 断连/取消也关闭底层连接，服务端据此 mark_truncated()。
      client.close();
    }
  }

  /// 把累积的 `data:` 行合成一帧并解析；非法 JSON 静默跳过，不打断整条流。
  SseEvent? _takeFrame(List<String> frameLines) {
    if (frameLines.isEmpty) return null;
    final String payload = frameLines.join('\n');
    frameLines.clear();
    try {
      final Object? decoded = jsonDecode(payload);
      if (decoded is Map<String, dynamic>) return SseEvent(decoded);
    } catch (_) {
      // 半包或坏帧：跳过。
    }
    return null;
  }

  /// POST /asr，上传 16k 单声道 16-bit WAV 字节，返回识别文本。
  ///
  /// 后端没起 ASR 服务时返回 503，这里抛异常由上层提示，打字不受影响。
  Future<String> asr(Uint8List wav) async {
    final http.Client client = http.Client();
    try {
      final http.Request request =
          http.Request('POST', Uri.parse('$baseUrl/asr'));
      request.headers['Content-Type'] = 'audio/wav';
      request.bodyBytes = wav;

      final http.StreamedResponse response = await client.send(request);
      final String body =
          await response.stream.transform(utf8.decoder).join();
      Map<String, dynamic>? data;
      try {
        final Object? decoded = jsonDecode(body);
        if (decoded is Map<String, dynamic>) data = decoded;
      } catch (_) {
        // 非 JSON 响应：下面按 HTTP 状态报错。
      }
      if (response.statusCode != 200 || data == null || data['ok'] != true) {
        final String detail = (data != null ? data['detail'] : null)?.toString() ??
            'HTTP ${response.statusCode}';
        throw Exception(detail);
      }
      final Object? text = data['text'];
      return text is String ? text.trim() : '';
    } finally {
      client.close();
    }
  }

  /// GET /app/version：服务端最新版本清单。连不上 / 非 200 返回 null。
  Future<Map<String, dynamic>?> appVersion() async {
    final http.Client client = http.Client();
    try {
      final http.Response response = await client
          .get(Uri.parse('$baseUrl/app/version'))
          .timeout(const Duration(seconds: 5));
      if (response.statusCode != 200) return null;
      final Object? decoded = jsonDecode(utf8.decode(response.bodyBytes));
      return decoded is Map<String, dynamic> ? decoded : null;
    } catch (_) {
      return null;
    } finally {
      client.close();
    }
  }

  /// POST /feedback：只报「对了 / 不对」，不写长文。
  ///
  /// 这一步是全系统最便宜的信号：一次点击，换它下次少错一次。
  /// 失败不抛给界面（反馈丢一次不算事故），但也不假装成功。
  Future<bool> feedback({
    required String requestId,
    required int verdict,
    int deliveryId = 0,
    String target = '',
  }) async {
    final http.Client client = http.Client();
    try {
      final String body = jsonEncode(<String, dynamic>{
        'request_id': requestId,
        'verdict': verdict,
        if (deliveryId > 0) 'delivery_id': deliveryId,
        'target': target.length > 120 ? target.substring(0, 120) : target,
      });
      final http.Response response = await client
          .post(
            Uri.parse('$baseUrl/feedback'),
            headers: <String, String>{'Content-Type': 'application/json'},
            body: body,
          )
          .timeout(const Duration(seconds: 8));
      return response.statusCode == 200;
    } catch (_) {
      return false;
    } finally {
      client.close();
    }
  }

  /// GET /proactive：取它「先开的口」。
  ///
  /// 取走即标记已送达（服务端负责），所以同一个 id 不会重复弹。
  /// 任何失败都返回空列表：主动开口不该制造错误提示。
  Future<List<Map<String, dynamic>>> proactive({String userId = 'u1'}) async {
    final http.Client client = http.Client();
    try {
      final http.Response response = await client
          .get(Uri.parse('$baseUrl/proactive?user_id=$userId'))
          .timeout(const Duration(seconds: 8));
      if (response.statusCode != 200) return const <Map<String, dynamic>>[];
      final Object? decoded = jsonDecode(utf8.decode(response.bodyBytes));
      final Object? items = decoded is Map ? decoded['items'] : null;
      if (items is List) {
        return items
            .whereType<Map>()
            .map((Map m) => Map<String, dynamic>.from(m))
            .toList(growable: false);
      }
      return const <Map<String, dynamic>>[];
    } catch (_) {
      return const <Map<String, dynamic>>[];
    } finally {
      client.close();
    }
  }

  /// POST /tts，返回原始 WAV 字节；非 200 抛异常，由上层静默降级。
  Future<Uint8List> tts({
    required String text,
    required String emotion,
    String? requestId,
    String? referenceAudioId,
  }) async {
    final http.Client client = http.Client();
    try {
      final http.Request request = http.Request('POST', Uri.parse('$baseUrl/tts'));
      request.headers['Content-Type'] = 'application/json';
      request.bodyBytes = utf8.encode(jsonEncode(<String, dynamic>{
        'text': text,
        'emotion': emotion,
        if (referenceAudioId != null) 'reference_audio_id': referenceAudioId,
        if (requestId != null) 'request_id': requestId,
      }));

      final http.StreamedResponse response =
          await client.send(request).timeout(const Duration(seconds: 25));
      if (response.statusCode != 200) {
        String detail = 'HTTP ${response.statusCode}';
        String? code;
        String body = '';
        try {
          body = await response.stream.transform(utf8.decoder).join();
          final Object? decoded = jsonDecode(body);
          if (decoded is Map) {
            final Object? d = decoded['detail'];
            if (d != null && '$d'.trim().isNotEmpty) detail = '$d';
            final Object? c = decoded['code'];
            if (c != null && '$c'.trim().isNotEmpty) code = '$c';
          }
        } catch (_) {
          // 非 JSON 的错误体：有纯文本就带上，否则保留状态码。
          if (body.trim().isNotEmpty) detail = body.trim();
        }
        // 后端 503 体形如 {"detail":"真实原因","code":"TTS_UNAVAILABLE"}；
        // detail 是给人看的原因，code 只作补充标注。
        final String label = code == null ? detail : '$detail（$code）';
        throw Exception('TTS 不可用：$label');
      }
      // 不依赖 ByteStream.toBytes()，逐块汇总，兼容各版本 http。
      final List<List<int>> chunks = await response.stream
          .toList()
          .timeout(const Duration(seconds: 60));
      final BytesBuilder builder = BytesBuilder(copy: false);
      for (final List<int> chunk in chunks) {
        builder.add(chunk);
      }
      return builder.takeBytes();
    } finally {
      client.close();
    }
  }
}
