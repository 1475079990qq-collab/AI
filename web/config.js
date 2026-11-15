/* companion Web 前端配置
 * 纯全局对象（非 ES module），保证 file:// 直接打开也能加载，不受 CORS 限制。
 * 默认按 location.hostname 推导两个服务地址；
 * 支持 ?brain=http://...&console=http://... 覆盖，便于手机局域网访问。
 */
(function () {
  'use strict';

  function trimSlash(u) {
    return String(u == null ? '' : u).replace(/\/+$/, '');
  }

  var host = (typeof location !== 'undefined' && location.hostname) || 'localhost';
  var search = (typeof location !== 'undefined' && location.search) || '';
  var qs = new URLSearchParams(search);

  var brainParam = trimSlash(qs.get('brain'));
  var consoleParam = trimSlash(qs.get('console'));

  // 端口由启动时生成的 config.local.js 提供，改 config.yaml 后重启即生效。
  var local = window.COMPANION_LOCAL || {};
  var brainPort = local.brain_port || 8000;
  var consolePort = local.console_port || 8001;

  var cfg = {
    BRAIN_URL: brainParam || ('http://' + host + ':' + brainPort),
    CONSOLE_URL: consoleParam || ('http://' + host + ':' + consolePort),
    // 由 URL 参数显式指定时，优先级高于 localStorage（并落地，保证跨页一致）
    BRAIN_OVERRIDE: brainParam || null,
    CONSOLE_OVERRIDE: consoleParam || null
  };

  window.COMPANION_CONFIG = cfg;
})();
