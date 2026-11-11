/* Shared AI settings dialog (OpenAI-compatible endpoint).
   Included by index.html and punct_app.html.  It talks only to the local app
   server; the API key is posted to the local server and masked on readback. */
(function () {
  'use strict';

  var root = null;
  var cfg = null;
  var busy = false;

  function qs(sel) { return root.querySelector(sel); }

  async function api(path, options) {
    var response = await fetch(path, Object.assign({
      headers: { 'Content-Type': 'application/json; charset=utf-8' }
    }, options || {}));
    var data = null;
    try { data = await response.json(); } catch (e) {}
    if (!response.ok) {
      var detail = data && (data.detail || data.message);
      if (Array.isArray(detail)) detail = detail.map(function (x) { return x.msg || x; }).join('; ');
      throw new Error(detail || ('HTTP ' + response.status));
    }
    return data;
  }

  function ensureDom() {
    if (root) return;
    root = document.createElement('div');
    root.id = 'guwenSettingsOverlay';
    root.className = 'gsw-overlay';
    root.hidden = true;
    root.innerHTML =
      '<div class="gsw-dialog" role="dialog" aria-modal="true" aria-labelledby="gswTitle">' +
        '<div class="gsw-head">' +
          '<div><div class="gsw-title" id="gswTitle">AI 句读设置</div>' +
          '<div class="gsw-sub">接入任意 OpenAI 兼容接口；应用本身不启动本地模型服务。</div></div>' +
          '<button type="button" class="gsw-close" data-gsw="close" title="关闭">×</button>' +
        '</div>' +
        '<div class="gsw-body">' +
          '<div class="gsw-row">' +
            '<label class="gsw-label" for="gswBase">接口地址<span class="gsw-opt">OpenAI 兼容 Base URL</span></label>' +
            '<div class="gsw-control">' +
              '<input type="text" id="gswBase" spellcheck="false" placeholder="https://api.openai.com/v1">' +
              '<div class="gsw-hint">填到 <b>/v1</b> 即可；也可直接粘贴完整的 <b>/chat/completions</b> 地址，保存时会自动去掉后缀。</div>' +
            '</div>' +
          '</div>' +
          '<div class="gsw-row">' +
            '<label class="gsw-label" for="gswKey">API Key<span class="gsw-opt">只保存在本机</span></label>' +
            '<div class="gsw-control">' +
              '<input type="password" id="gswKey" spellcheck="false" autocomplete="new-password" placeholder="可选；无鉴权的本地服务留空">' +
              '<label class="gsw-check"><input type="checkbox" id="gswClearKey"> 清除已保存的密钥（保存后生效）</label>' +
              '<div class="gsw-hint" id="gswKeyHint"></div>' +
            '</div>' +
          '</div>' +
          '<div class="gsw-row">' +
            '<label class="gsw-label" for="gswModel">模型<span class="gsw-opt">模型 ID</span></label>' +
            '<div class="gsw-control">' +
              '<div class="gsw-inputline">' +
                '<input type="text" id="gswModel" spellcheck="false" list="gswModelList" placeholder="例如 gpt-5.1 / deepseek-chat / qwen3-max">' +
                '<datalist id="gswModelList"></datalist>' +
                '<button type="button" class="gsw-btn" data-gsw="models">获取模型</button>' +
              '</div>' +
              '<select id="gswModelPick" style="display:none;margin-top:6px"></select>' +
              '<div class="gsw-hint">接口支持 <b>/models</b> 时可一键拉取并从下拉框选择；返回 404 也可直接手填模型名。</div>' +
            '</div>' +
          '</div>' +
          '<div class="gsw-row">' +
            '<label class="gsw-label" for="gswEffort">思考强度<span class="gsw-opt">reasoning_effort</span></label>' +
            '<div class="gsw-control">' +
              '<select id="gswEffort">' +
                '<option value="">不发送（默认）</option>' +
                '<option value="none">none（关闭，若模型支持）</option>' +
                '<option value="minimal">minimal（最低）</option>' +
                '<option value="low">low（低）</option>' +
                '<option value="medium">medium（中）</option>' +
                '<option value="high">high（高）</option>' +
              '</select>' +
              '<div class="gsw-hint">按 OpenAI 标准作为 <b>reasoning_effort</b> 发送。供应商使用 <b>thinking</b> 等私有字段时，在下方“额外参数”里配置。</div>' +
            '</div>' +
          '</div>' +
          '<div class="gsw-row">' +
            '<label class="gsw-label" for="gswTemp">采样温度<span class="gsw-opt">temperature</span></label>' +
            '<div class="gsw-control">' +
              '<input type="text" id="gswTemp" inputmode="decimal" placeholder="留空 = 服务端默认；古籍标点建议 0">' +
              '<div class="gsw-hint">部分推理模型只接受默认温度，报错时请留空。</div>' +
            '</div>' +
          '</div>' +
          '<div class="gsw-row">' +
            '<label class="gsw-label" for="gswTimeout">请求超时<span class="gsw-opt">秒</span></label>' +
            '<div class="gsw-control">' +
              '<input type="number" id="gswTimeout" min="5" max="3600" step="1" value="600">' +
            '</div>' +
          '</div>' +
          '<div class="gsw-row">' +
            '<label class="gsw-label">高级<span class="gsw-opt">供应商私有参数</span></label>' +
            '<div class="gsw-control">' +
              '<details class="gsw-details">' +
                '<summary>额外请求参数 JSON（可选）</summary>' +
                '<textarea id="gswExtra" spellcheck="false" placeholder=\'{"thinking": {"type": "disabled"}}\'></textarea>' +
                '<div class="gsw-hint">会合并进请求体，用来适配 DeepSeek / Qwen / GLM 等供应商的思考开关；不能覆盖 model / messages / stream。</div>' +
              '</details>' +
            '</div>' +
          '</div>' +
          '<div class="gsw-status" id="gswStatus"></div>' +
          '<div class="gsw-note"><b>用法：</b>保存并测试成功后，回到句读台点「句读整本初版」。正文会发送到你配置的接口；书稿、图片和 API Key 仍只保存在本机。</div>' +
          '<div class="gsw-actions">' +
            '<button type="button" class="gsw-btn ghost" data-gsw="test">测试连接</button>' +
            '<button type="button" class="gsw-btn" data-gsw="close">取消</button>' +
            '<button type="button" class="gsw-btn primary" data-gsw="save">保存设置</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(root);

    root.addEventListener('click', function (event) {
      var action = event.target && event.target.getAttribute && event.target.getAttribute('data-gsw');
      if (action === 'close' || event.target === root) { close(); return; }
      if (action === 'models') { fetchModels(); return; }
      if (action === 'test') { testConnection(); return; }
      if (action === 'save') { save(); }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && root && !root.hidden) close();
    });
    var clear = qs('#gswClearKey');
    if (clear) {
      clear.addEventListener('change', function () {
        var key = qs('#gswKey');
        key.disabled = clear.checked;
        if (clear.checked) key.value = '';
        setKeyHint();
      });
    }
    var keyInput = qs('#gswKey');
    if (keyInput) keyInput.addEventListener('input', setKeyHint);
    var pick = qs('#gswModelPick');
    if (pick) {
      pick.addEventListener('change', function () {
        if (pick.value) qs('#gswModel').value = pick.value;
      });
    }
  }

  function setStatus(text, kind) {
    var node = qs('#gswStatus');
    if (!node) return;
    node.textContent = text || '';
    node.className = 'gsw-status' + (text ? ' show' : '') + (kind ? ' ' + kind : '');
  }

  function setBusy(value) {
    busy = !!value;
    if (!root) return;
    root.querySelectorAll('button, input, select, textarea').forEach(function (node) {
      if (node.getAttribute('data-gsw') === 'close') return;
      node.disabled = busy;
    });
    var clear = qs('#gswClearKey');
    if (clear) clear.disabled = busy || !(cfg && cfg.has_api_key);
    var key = qs('#gswKey');
    if (key) key.disabled = busy || !!(clear && clear.checked);
  }

  function setKeyHint() {
    var hint = qs('#gswKeyHint');
    if (!hint) return;
    if (!cfg) { hint.textContent = ''; return; }
    if (qs('#gswClearKey').checked) {
      hint.textContent = '保存后将删除本机保存的密钥。';
      return;
    }
    hint.textContent = cfg.api_key_hint || (cfg.has_api_key ? '已保存密钥；留空保持不变。' : '本机尚未保存密钥。');
  }

  function fill(config) {
    cfg = config || {};
    qs('#gswBase').value = cfg.base_url || '';
    qs('#gswModel').value = cfg.model || '';
    var pick = qs('#gswModelPick');
    if (pick) { pick.style.display = 'none'; pick.innerHTML = ''; }
    qs('#gswEffort').value = cfg.reasoning_effort || '';
    qs('#gswTemp').value = cfg.temperature == null ? '' : String(cfg.temperature);
    qs('#gswTimeout').value = cfg.timeout || 600;
    qs('#gswExtra').value = cfg.extra_body || '';
    qs('#gswKey').value = '';
    qs('#gswKey').placeholder = cfg.has_api_key ? '已保存；留空保持不变' : '可选；无鉴权的本地服务留空';
    qs('#gswClearKey').checked = false;
    qs('#gswClearKey').disabled = !cfg.has_api_key;
    qs('#gswKey').disabled = false;
    setKeyHint();
  }

  async function load() {
    setStatus('正在读取本机设置…', 'busy');
    try {
      fill(await api('/api/llm/config'));
      setStatus('', '');
    } catch (error) {
      setStatus('读取设置失败：' + error.message, 'err');
    }
  }

  function payload() {
    var data = {
      base_url: qs('#gswBase').value.trim(),
      model: qs('#gswModel').value.trim(),
      reasoning_effort: qs('#gswEffort').value,
      temperature: qs('#gswTemp').value.trim(),
      timeout: Number(qs('#gswTimeout').value || 600),
      extra_body: qs('#gswExtra').value.trim()
    };
    if (qs('#gswClearKey').checked) {
      data.clear_api_key = true;
    } else {
      var key = qs('#gswKey').value.trim();
      if (key) data.api_key = key;
    }
    return data;
  }

  async function fetchModels() {
    if (busy) return;
    var base = qs('#gswBase').value.trim();
    if (!base) { setStatus('请先填写接口地址。', 'err'); return; }
    setBusy(true);
    setStatus('正在从接口读取模型列表…', 'busy');
    try {
      var result = await api('/api/llm/models', {
        method: 'POST',
        body: JSON.stringify(payload())
      });
      var list = qs('#gswModelList');
      list.innerHTML = '';
      var pick = qs('#gswModelPick');
      pick.innerHTML = '<option value="">从模型列表选择…</option>';
      (result.models || []).forEach(function (name) {
        var option = document.createElement('option');
        option.value = name;
        list.appendChild(option);
        pick.appendChild(option.cloneNode(true));
      });
      pick.style.display = 'block';
      var current = qs('#gswModel').value.trim();
      if (current && (result.models || []).indexOf(current) >= 0) pick.value = current;
      setStatus('找到 ' + (result.models || []).length + ' 个模型，可从下拉框选择或直接输入筛选。', 'ok');
    } catch (error) {
      setStatus('获取模型失败：' + error.message, 'err');
    } finally {
      setBusy(false);
    }
  }

  async function testConnection() {
    if (busy) return;
    if (!qs('#gswBase').value.trim()) { setStatus('请先填写接口地址。', 'err'); return; }
    if (!qs('#gswModel').value.trim()) { setStatus('请先选择或填写模型名。', 'err'); return; }
    setBusy(true);
    setStatus('正在发起一次最小请求…', 'busy');
    try {
      var result = await api('/api/llm/test', { method: 'POST', body: JSON.stringify(payload()) });
      setStatus('连接成功 · 模型 ' + (result.model || '') + ' · ' + result.latency_ms + ' ms · 返回：' + (result.reply || '(空)'), 'ok');
    } catch (error) {
      setStatus('连接失败：' + error.message, 'err');
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (busy) return;
    setBusy(true);
    setStatus('正在保存…', 'busy');
    try {
      var result = await api('/api/llm/config', { method: 'PUT', body: JSON.stringify(payload()) });
      fill(result);
      setStatus('已保存。建议再点「测试连接」确认模型可用。', 'ok');
    } catch (error) {
      setStatus('保存失败：' + error.message, 'err');
    } finally {
      setBusy(false);
    }
  }

  function open() {
    ensureDom();
    root.hidden = false;
    load().then(function () {
      var base = qs('#gswBase');
      if (base) base.focus();
    });
  }

  function close() {
    if (!root) return;
    root.hidden = true;
  }

  window.GuWenSettings = { open: open, close: close };
})();
