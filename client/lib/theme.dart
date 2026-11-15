/// 视觉基线：米白纸感 + 墨色 + 一处暖光。
///
/// 「小资」不是加装饰，是减法：低饱和、大留白、慢动作、衬线。
/// 这里只放色值和文本样式，所有页面只从这里取色，避免同一颜色出现十种写法。
import 'package:flutter/material.dart';

class Palette {
  // 纸
  static const Color paper = Color(0xFFF5F1EA);
  static const Color paperDeep = Color(0xFFEAE3D6);
  static const Color card = Color(0xFFFBF8F2);

  // 墨
  static const Color ink = Color(0xFF1F1B17);
  static const Color inkSoft = Color(0xFF4A423A);

  // 低饱和点缀（一处只用一个）
  static const Color warmGrey = Color(0xFFC9B8A6);
  static const Color mistBlue = Color(0xFF8FA6B2);
  static const Color olive = Color(0xFF7C8B6B);
  static const Color terracotta = Color(0xFFC97A4E);

  // 文本
  static const Color textPrimary = Color(0xFF23201C);
  static const Color textMuted = Color(0xFF7D7469);
  static const Color hairline = Color(0xFFDCD3C4);

  // 夜间（一盏灯，不是黑屏）
  static const Color night = Color(0xFF171614);
  static const Color nightSurface = Color(0xFF211F1C);
  static const Color nightText = Color(0xFFEDE7DD);
  static const Color nightMuted = Color(0xFF9A9188);
}

/// 文本样式：标题走衬线（"serif" 在中文设备上会落到 Noto Serif CJK），
/// 正文保持无衬线但放大行距——衬线只用来「定调」，不用来打满整屏。
class Type {
  static const String serif = 'serif';

  static const TextStyle title = TextStyle(
    fontFamily: serif,
    fontSize: 21,
    height: 1.45,
    letterSpacing: 0.4,
    color: Palette.textPrimary,
  );

  static const TextStyle quote = TextStyle(
    fontFamily: serif,
    fontSize: 16,
    height: 1.85,
    letterSpacing: 0.3,
    color: Palette.textPrimary,
  );

  static const TextStyle body = TextStyle(
    fontSize: 15,
    height: 1.7,
    letterSpacing: 0.2,
    color: Palette.textPrimary,
  );

  static const TextStyle caption = TextStyle(
    fontSize: 12,
    height: 1.5,
    letterSpacing: 0.2,
    color: Palette.textMuted,
  );
}
