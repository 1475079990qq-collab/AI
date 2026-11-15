/// 角色：一滴墨。
///
/// 为什么不是人形：人形会让人拿「像不像人」去量它，一旦不像就一直别扭。
/// 墨滴没有五官、没有四肢，只有一个载体同时承接三件事——
///   呼吸（它在，陪着你）、涟漪（它在说）、分裂出一枚薄片（它把东西递给你）。
/// 第三件是这套产品最缺的那一环：它一直只会「说」，不会「给」。
///
/// 六层绘制，全部矢量，不依赖位图；屏宽占比 ≤46%，上下留白 ≥38%。
import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'theme.dart';

class InkDrop extends StatefulWidget {
  const InkDrop({
    super.key,
    required this.tint,
    this.smile = 0.0,
    required this.talking,
    this.delivering = false,
    this.size = 150,
    this.dark = false,
  });

  /// 情绪色（只用于极淡的晕，不许抢占墨的主体）
  final Color tint;

  /// -1（沉）～ 1（轻）：影响呼吸深度与墨滴上沿的尖锐度
  final double smile;

  final bool talking;

  /// 有东西递出去时，分裂一枚薄片
  final bool delivering;

  final double size;
  final bool dark;

  @override
  State<InkDrop> createState() => _InkDropState();
}

class _InkDropState extends State<InkDrop> with TickerProviderStateMixin {
  // 呼吸一周 4.4 秒：比真人呼吸（约 3 秒）慢，慢就是高级感。
  late final AnimationController _breath = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 4400),
  )..repeat();

  // 说话时的涟漪，只在说话时跑，停下就归零，不匀速抖动。
  late final AnimationController _ripple = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1400),
  );

  // 光晕明暗：代替「眨眼」，5.6 秒里暗 0.6 秒。
  late final AnimationController _glow = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 5600),
  )..repeat();

  // 递出薄片：一次动作 1.2 秒，做完就停在末态。
  late final AnimationController _deliver = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1200),
  );

  @override
  void initState() {
    super.initState();
    if (widget.talking) _ripple.repeat();
    if (widget.delivering) _deliver.forward();
  }

  @override
  void didUpdateWidget(covariant InkDrop old) {
    super.didUpdateWidget(old);
    if (widget.talking && !_ripple.isAnimating) {
      _ripple.repeat();
    } else if (!widget.talking && _ripple.isAnimating) {
      _ripple.stop();
      _ripple.value = 0;
    }
    if (widget.delivering && !old.delivering) {
      _deliver.forward(from: 0);
    } else if (!widget.delivering && old.delivering) {
      _deliver.value = 0;
    }
  }

  @override
  void dispose() {
    _breath.dispose();
    _ripple.dispose();
    _glow.dispose();
    _deliver.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: widget.size,
      height: widget.size,
      child: AnimatedBuilder(
        animation: Listenable.merge(<Listenable>[_breath, _ripple, _glow, _deliver]),
        builder: (BuildContext context, _) {
          // 呼吸曲线：正弦，两端自然停顿。
          final double b = 0.5 - 0.5 * math.cos(_breath.value * 2 * math.pi);
          // 光晕：循环末尾 10% 的时间里压暗，像一次极轻的「眨眼」。
          final double g = _glow.value;
          final double dim = g > 0.9 ? (1 - (g - 0.9) / 0.1) : 1.0;
          return CustomPaint(
            painter: _InkDropPainter(
              breath: b,
              ripple: widget.talking ? _ripple.value : 0.0,
              glowDim: 0.55 + 0.45 * dim,
              deliver: _deliver.value,
              tint: widget.tint,
              smile: widget.smile,
              dark: widget.dark,
            ),
          );
        },
      ),
    );
  }
}

class _InkDropPainter extends CustomPainter {
  _InkDropPainter({
    required this.breath,
    required this.ripple,
    required this.glowDim,
    required this.deliver,
    required this.tint,
    required this.smile,
    required this.dark,
  });

  final double breath;      // 0..1
  final double ripple;      // 0..1
  final double glowDim;     // 0.55..1
  final double deliver;     // 0..1
  final Color tint;
  final double smile;
  final bool dark;

  @override
  void paint(Canvas canvas, Size size) {
    final double w = size.width;
    final double h = size.height;
    final double cx = w / 2;
    // 上下各留 16% 空白，墨滴只占中间 68% 的高度
    final double top = h * 0.16;
    final double bottom = h * 0.84;
    final double floatY = math.sin(breath * math.pi) * h * 0.012;
    final double body = 1.0 + breath * 0.014;

    canvas.save();
    canvas.translate(0, floatY);

    // ---- 1) 外晕：情绪色只在这里出现，极淡，跟着呼吸起伏 ----
    final double glowR = w * (0.36 + breath * 0.02);
    final Rect glowRect = Rect.fromCircle(center: Offset(cx, h * 0.56), radius: glowR);
    canvas.drawCircle(
      Offset(cx, h * 0.56),
      glowR,
      Paint()
        ..shader = RadialGradient(
          colors: <Color>[
            tint.withValues(alpha: 0.16 * glowDim),
            tint.withValues(alpha: 0.05 * glowDim),
            tint.withValues(alpha: 0.0),
          ],
          stops: const <double>[0.0, 0.55, 1.0],
        ).createShader(glowRect),
    );

    // ---- 2) 墨滴主体：上尖下圆，随情绪决定「头」有多尖 ----
    final double sharpness = 0.10 + 0.05 * (1 - smile) / 2;
    final double halfW = w * 0.19 * body;
    final double bellyY = h * 0.62;
    final double tipY = top + h * (sharpness);

    final Path drop = Path()
      ..moveTo(cx, tipY)
      ..quadraticBezierTo(cx - halfW * 0.55, h * 0.38, cx - halfW, bellyY - h * 0.04)
      ..quadraticBezierTo(cx - halfW * 1.02, h * 0.74, cx, bottom)
      ..quadraticBezierTo(cx + halfW * 1.02, h * 0.74, cx + halfW, bellyY - h * 0.04)
      ..quadraticBezierTo(cx + halfW * 0.55, h * 0.38, cx, tipY)
      ..close();

    final Rect bodyRect = Rect.fromLTRB(cx - halfW, tipY, cx + halfW, bottom);
    final Color cTop = dark ? const Color(0xFFE9E3D8) : const Color(0xFF3A332C);
    final Color cMid = dark ? const Color(0xFFCFC7BA) : const Color(0xFF221D19);
    final Color cBottom = dark ? const Color(0xFF9E968A) : const Color(0xFF14110E);
    canvas.drawPath(
      drop,
      Paint()
        ..shader = LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: <Color>[
            cTop.withValues(alpha: dark ? 0.55 : 0.92),
            cMid.withValues(alpha: dark ? 0.75 : 0.98),
            cBottom.withValues(alpha: dark ? 0.85 : 1.0),
          ],
          stops: const <double>[0.0, 0.55, 1.0],
        ).createShader(bodyRect),
    );

    // ---- 3) 墨里的暗涌：一层略小的墨，跟着呼吸微微位移，让它「活着」 ----
    final double innerScale = 0.86 + breath * 0.03;
    final Path inner = Path()
      ..addOval(Rect.fromCenter(
        center: Offset(cx, bellyY + h * 0.02),
        width: halfW * 2 * innerScale,
        height: h * 0.26 * innerScale,
      ));
    canvas.drawPath(
      inner,
      Paint()..color = (dark ? Colors.white : Colors.black).withValues(alpha: 0.06),
    );

    // ---- 4) 高光：只有一点，位置偏左上，像纸上的光 ----
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx - halfW * 0.34, h * 0.42),
        width: w * 0.075,
        height: h * 0.10,
      ),
      Paint()
        ..color = (dark ? Colors.white : const Color(0xFFFFFDF8))
            .withValues(alpha: dark ? 0.14 : 0.20),
    );

    // ---- 5) 说话时的涟漪：三圈，随说话节奏外扩，说完就消失 ----
    if (ripple > 0 && ripple < 1) {
      for (int i = 0; i < 3; i++) {
        final double t = (ripple + i * 0.33) % 1.0;
        final double r = halfW * (1.15 + t * 1.55);
        canvas.drawOval(
          Rect.fromCenter(
            center: Offset(cx, bellyY + h * 0.06),
            width: r * 2,
            height: r * 1.35,
          ),
          Paint()
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.0
            ..color = (dark ? const Color(0xFFEDE7DD) : const Color(0xFF3A332C))
                .withValues(alpha: 0.16 * (1 - t)),
        );
      }
    }

    // ---- 6) 递出去：一缕墨丝断开，分裂出一枚薄片 ----
    if (deliver > 0) {
      final double p = Curves.easeOutCubic.transform(deliver.clamp(0.0, 1.0));
      final Offset sheetCenter = Offset(cx + halfW * (0.9 + 1.5 * p), h * 0.60 - h * 0.04 * p);
      final double sheetW = w * 0.20;
      final double sheetH = h * 0.24;

      // 墨丝：从墨滴右缘连到薄片，p 越大越细
      if (p < 0.85) {
        final Path thread = Path()
          ..moveTo(cx + halfW * 0.92, bellyY)
          ..quadraticBezierTo(
            cx + halfW * (1.2 + 0.8 * p), bellyY - h * 0.02,
            sheetCenter.dx - sheetW * 0.5, sheetCenter.dy,
          );
        canvas.drawPath(
          thread,
          Paint()
            ..style = PaintingStyle.stroke
            ..strokeWidth = 2.2 * (1 - p)
            ..color = (dark ? const Color(0xFFEDE7DD) : const Color(0xFF2A231E))
                .withValues(alpha: 0.45 * (1 - p)),
        );
      }

      // 薄片：一张能点开的纸，轻微倾斜，右下角带一道极轻的阴影
      canvas.save();
      canvas.translate(sheetCenter.dx, sheetCenter.dy);
      canvas.rotate(-0.10);
      final RRect sheet = RRect.fromRectAndRadius(
        Rect.fromCenter(center: Offset.zero, width: sheetW, height: sheetH),
        Radius.circular(w * 0.02),
      );
      canvas.drawRRect(
        sheet.shift(Offset(w * 0.008, h * 0.008)),
        Paint()..color = Colors.black.withValues(alpha: 0.10 * p),
      );
      canvas.drawRRect(
        sheet,
        Paint()..color = (dark ? const Color(0xFF2A2724) : const Color(0xFFFDFBF6))
            .withValues(alpha: 0.92 * p),
      );
      canvas.drawRRect(
        sheet,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 0.8
          ..color = (dark ? const Color(0xFF8C8478) : Palette.hairline)
              .withValues(alpha: 0.9 * p),
      );
      // 两行占位短线：暗示「上面有内容」，不写具体字
      for (int i = 0; i < 2; i++) {
        canvas.drawLine(
          Offset(-sheetW * 0.30, -sheetH * 0.06 + i * sheetH * 0.20),
          Offset(sheetW * 0.30, -sheetH * 0.06 + i * sheetH * 0.20),
          Paint()
            ..strokeWidth = 1.0
            ..color = (dark ? const Color(0xFF9A9188) : Palette.textMuted)
                .withValues(alpha: 0.55 * p),
        );
      }
      canvas.restore();
    }

    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant _InkDropPainter old) {
    return old.breath != breath ||
        old.ripple != ripple ||
        old.glowDim != glowDim ||
        old.deliver != deliver ||
        old.tint != tint ||
        old.smile != smile ||
        old.dark != dark;
  }
}
