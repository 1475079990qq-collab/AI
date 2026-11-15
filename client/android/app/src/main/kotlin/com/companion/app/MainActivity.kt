package com.companion.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.ByteArrayOutputStream
import kotlin.concurrent.thread

/**
 * 按住说话录音（16k / 单声道 / 16-bit PCM WAV）。
 *
 * 自己用 AudioRecord 实现，不引第三方录音插件：那类插件多半带 CMake 原生库，
 * 而本项目位于中文目录（E:\情感开发），CMake 的 JSON 转义会直接崩。
 * 这里只输出后端 /asr 认的 16-bit PCM WAV，零转码。
 */
class MainActivity : FlutterActivity() {
    private val channelName = "companion/recorder"
    private val shellChannelName = "companion/shell"
    private var recorder: AudioRecord? = null
    private var recordingThread: Thread? = null
    private val pcm = ByteArrayOutputStream()
    @Volatile private var recording = false
    private var pendingResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channelName)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "start" -> startRecording(result)
                    "stop" -> result.success(stopRecording())
                    "cancel" -> {
                        cancelRecording()
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            }
        // 更新提示只用到两件事：读本机版本号、把下载地址丢给浏览器。
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, shellChannelName)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "appVersion" -> result.success(appVersion())
                    "openUrl" -> result.success(openUrl(call.argument<String>("url")))
                    else -> result.notImplemented()
                }
            }
    }

    /** 本机 versionCode / versionName，Dart 侧和 /app/version 比大小。 */
    private fun appVersion(): Map<String, Any> {
        val info = packageManager.getPackageInfo(packageName, 0)
        val code: Long = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            @Suppress("DEPRECATION")
            info.versionCode.toLong()
        }
        return mapOf("code" to code, "name" to (info.versionName ?: ""))
    }

    /** 交给系统浏览器下载 APK（安装时需允许「安装未知应用」）。 */
    private fun openUrl(url: String?): Boolean {
        if (url.isNullOrBlank()) return false
        return try {
            val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            startActivity(intent)
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun hasPermission(): Boolean =
        checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun startRecording(result: MethodChannel.Result) {
        if (recording) {
            result.success(false)
            return
        }
        if (!hasPermission()) {
            pendingResult = result
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 9001)
            return
        }
        result.success(begin())
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 9001) {
            val granted =
                grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED
            pendingResult?.success(granted && begin())
            pendingResult = null
        }
    }

    private fun begin(): Boolean {
        val minBuf = AudioRecord.getMinBufferSize(
            16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) return false
        val source = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        } else {
            MediaRecorder.AudioSource.MIC
        }
        val rec = AudioRecord(
            source, 16000, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, minBuf * 2
        )
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            return false
        }
        synchronized(pcm) { pcm.reset() }
        rec.startRecording()
        recorder = rec
        recording = true
        recordingThread = thread(name = "companion-recorder") {
            val buf = ByteArray(minBuf)
            while (recording) {
                val n = rec.read(buf, 0, buf.size)
                if (n > 0) synchronized(pcm) { pcm.write(buf, 0, n) }
            }
        }
        return true
    }

    private fun stopRecording(): ByteArray? {
        if (!recording) return null
        recording = false
        // 先 stop 让阻塞中的 read() 立即返回，再等线程退出，最后 release，避免并发读已释放对象。
        recorder?.let {
            try {
                it.stop()
            } catch (_: Exception) {
            }
        }
        try {
            recordingThread?.join(800)
        } catch (_: InterruptedException) {
        }
        recordingThread = null
        recorder?.let {
            try {
                it.release()
            } catch (_: Exception) {
            }
        }
        recorder = null
        val data = synchronized(pcm) { pcm.toByteArray() }
        synchronized(pcm) { pcm.reset() }
        return wavBytes(data)
    }

    private fun cancelRecording() {
        if (!recording) return
        recording = false
        recorder?.let {
            try {
                it.stop()
            } catch (_: Exception) {
            }
        }
        try {
            recordingThread?.join(800)
        } catch (_: InterruptedException) {
        }
        recordingThread = null
        recorder?.let {
            try {
                it.release()
            } catch (_: Exception) {
            }
        }
        recorder = null
        synchronized(pcm) { pcm.reset() }
    }

    /** 给 PCM 数据加 44 字节 WAV 头，和后端 _decode_wav 的预期完全一致。 */
    private fun wavBytes(pcmData: ByteArray): ByteArray {
        val sampleRate = 16000
        val out = ByteArrayOutputStream(44 + pcmData.size)
        fun le32(v: Int) {
            out.write(v and 0xff)
            out.write((v shr 8) and 0xff)
            out.write((v shr 16) and 0xff)
            out.write((v shr 24) and 0xff)
        }

        fun le16(v: Int) {
            out.write(v and 0xff)
            out.write((v shr 8) and 0xff)
        }

        out.write("RIFF".toByteArray())
        le32(36 + pcmData.size)
        out.write("WAVE".toByteArray())
        out.write("fmt ".toByteArray())
        le32(16)
        le16(1)              // PCM
        le16(1)              // 单声道
        le32(sampleRate)
        le32(sampleRate * 2)
        le16(2)              // 块对齐
        le16(16)             // 位深
        out.write("data".toByteArray())
        le32(pcmData.size)
        out.write(pcmData)
        return out.toByteArray()
    }
}
