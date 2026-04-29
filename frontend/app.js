const MAX_PAPERS = 10;

const state = {
  papers: [],
  eventSource: null,
  pollTimer: null,
  jobState: "idle",
  jobId: null,
  eventOffset: 0,
  focusTerm: null,
  catalog: null,
  currentOrder: null,
  currentEstimate: null,
  paymentBusy: false,
  currentUser: null,
  authBusy: false,
};

const els = {
  pages: document.querySelectorAll("[data-page-panel]"),
  dropzone: document.querySelector("#dropzone"),
  dropHint: document.querySelector("#dropHint"),
  fileInput: document.querySelector("#fileInput"),
  paperList: document.querySelector("#paperList"),
  paperCounter: document.querySelector("#paperCounter"),
  startButton: document.querySelector("#startButton"),
  pricingButton: document.querySelector("#pricingButton"),
  resetButton: document.querySelector("#resetButton"),
  authEmailInput: document.querySelector("#authEmailInput"),
  authPasswordInput: document.querySelector("#authPasswordInput"),
  loginButton: document.querySelector("#loginButton"),
  registerButton: document.querySelector("#registerButton"),
  logoutButton: document.querySelector("#logoutButton"),
  authStatusText: document.querySelector("#authStatusText"),
  progressFill: document.querySelector("#progressFill"),
  progressLabel: document.querySelector("#progressLabel"),
  stageLabel: document.querySelector("#stageLabel"),
  chunkProgress: document.querySelector("#chunkProgress"),
  statusDot: document.querySelector("#statusDot"),
  statusText: document.querySelector("#statusText"),
  downloadLink: document.querySelector("#downloadLink"),
  providerSelect: document.querySelector("#providerSelect"),
  modelSelect: document.querySelector("#modelSelect"),
  estimatedCharsInput: document.querySelector("#estimatedCharsInput"),
  outputRatioInput: document.querySelector("#outputRatioInput"),
  pricingInline: document.querySelector("#pricingInline"),
  guideStartButton: document.querySelector("#guideStartButton"),
  homeButton: document.querySelector("#homeButton"),
  pricingModal: document.querySelector("#pricingModal"),
  pricingModalClose: document.querySelector("#pricingModalClose"),
  pricingReceipt: document.querySelector("#pricingReceipt"),
  pricingModalSubtitle: document.querySelector("#pricingModalSubtitle"),
  wechatPayButton: document.querySelector("#wechatPayButton"),
  alipayButton: document.querySelector("#alipayButton"),
  template: document.querySelector("#paperTemplate"),
};

const stageTranslations = {
  queued: "任务已创建",
  upload: "正在处理上传文件",
  preprocess: "正在预处理 LaTeX",
  translate: "正在翻译正文",
  postprocess: "正在整理引用和编号",
  done: "转换完成",
  failed: "转换失败",
};

const messageTranslations = {
  "Queued for processing": "任务已进入排队队列",
  "Worker started": "任务已进入转换队列",
  "Pipeline started": "转换流程已启动",
  "Collecting input folders": "正在读取上传文件",
  "Preprocess complete": "预处理完成",
  "Preparing translation": "正在准备翻译任务",
  "Translation batches prepared": "翻译批次已准备",
  "Translating paragraphs": "正在翻译段落",
  "Computing title translations": "正在翻译标题",
  "Building labels and references": "正在整理标签和引用",
  "DOCX render complete": "Word 文档已生成",
};

function formatMoney(value, currency = "CNY") {
  const amount = Number(value || 0);
  const safe = Number.isFinite(amount) ? amount : 0;
  const prefix = currency === "USD" ? "$" : "¥";
  return `${prefix}${safe.toFixed(2)}`;
}

function formatInteger(value) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(Number(value || 0));
}

function parsePositiveNumber(raw, fallback) {
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function getProviderInfo(providerId = els.providerSelect.value) {
  return state.catalog?.providers?.find((provider) => provider.id === providerId) || null;
}

function getModelInfo(providerId = els.providerSelect.value, modelId = els.modelSelect.value) {
  const provider = getProviderInfo(providerId);
  return provider?.models?.find((model) => model.id === modelId) || null;
}

function estimateDocument() {
  const provider = getProviderInfo();
  const model = getModelInfo();
  if (!provider || !model) return null;

  const relevantBytes = state.papers.reduce((total, paper) => {
    return total + paper.entries.reduce((sum, entry) => {
      if (!entry || entry.isDir) return sum;
      const name = String(entry.name || "").toLowerCase();
      if (name.endsWith(".tex")) return sum + Number(entry.size || 0);
      if (name.endsWith(".bib") || name.endsWith(".bbl")) return sum + Math.round(Number(entry.size || 0) * 0.45);
      return sum;
    }, 0);
  }, 0);

  const estimatedChars = Math.max(0, Math.round(relevantBytes * 0.92));
  const outputRatio = parsePositiveNumber(model.default_output_ratio, 1.1);
  const charsPerToken = parsePositiveNumber(model.chars_per_token, 1.7);
  const inputTokens = Math.max(1, Math.ceil(estimatedChars / charsPerToken));
  const outputTokens = Math.max(1, Math.ceil((estimatedChars * outputRatio) / charsPerToken));
  const inputCost = (inputTokens / 1_000_000) * Number(model.input_price_per_mtok || 0);
  const outputCost = (outputTokens / 1_000_000) * Number(model.output_price_per_mtok || 0);
  const rawCost = inputCost + outputCost;
  const markupMultiplier = parsePositiveNumber(model.markup_multiplier, 2.2);
  const minimumPrice = parsePositiveNumber(model.minimum_price, 2.0);
  const suggestedPrice = Math.max(minimumPrice, rawCost * markupMultiplier);

  return {
    provider,
    model,
    estimatedChars,
    outputRatio,
    inputTokens,
    outputTokens,
    inputCost,
    outputCost,
    rawCost,
    markupMultiplier,
    minimumPrice,
    suggestedPrice,
    currency: provider.currency || "CNY",
  };
}

function renderInlinePricing() {
  const estimate = estimateDocument();
  if (!estimate) {
    els.pricingInline.innerHTML = '<p class="field-note is-error">模型目录加载失败，暂时无法估价。</p>';
    return;
  }
  state.currentEstimate = estimate;
  els.estimatedCharsInput.value = String(estimate.estimatedChars || 0);
  els.outputRatioInput.value = estimate.outputRatio.toFixed(2);
  const paymentStatus = state.currentOrder
    ? state.currentOrder.status === "paid"
      ? "已支付，可开始转换"
      : state.currentOrder.status === "processing"
        ? "订单已使用，任务处理中"
      : "订单待支付"
    : state.currentUser
      ? "未生成订单"
      : "请先登录";
  els.pricingInline.innerHTML = `
    <div class="pricing-card">
      <div class="pricing-topline">
        <strong>${estimate.provider.label} · ${estimate.model.label}</strong>
        <span>${state.currentOrder ? `订单价 ${formatMoney(state.currentOrder.amount, state.currentOrder.currency)}` : `参考售价 ${formatMoney(estimate.suggestedPrice, estimate.currency)}`}</span>
      </div>
      <div class="pricing-grid">
        <span>预估字数</span><strong>${formatInteger(estimate.estimatedChars)}</strong>
        <span>输入 tokens</span><strong>${formatInteger(estimate.inputTokens)}</strong>
        <span>输出 tokens</span><strong>${formatInteger(estimate.outputTokens)}</strong>
        <span>模型成本</span><strong>${formatMoney(estimate.rawCost, estimate.currency)}</strong>
        <span>支付状态</span><strong>${paymentStatus}</strong>
      </div>
      <p class="field-note">${estimate.model.description} 价格日期：${estimate.provider.pricing_as_of}。${estimate.provider.pricing_note}</p>
    </div>
  `;
}

function renderAuthState(message = "") {
  const loggedIn = Boolean(state.currentUser);
  els.loginButton.disabled = state.authBusy || loggedIn;
  els.registerButton.disabled = state.authBusy || loggedIn;
  els.logoutButton.disabled = state.authBusy || !loggedIn;
  els.authEmailInput.disabled = state.authBusy || loggedIn;
  els.authPasswordInput.disabled = state.authBusy || loggedIn;
  els.authStatusText.textContent = message || (
    loggedIn
      ? `已登录：${state.currentUser.email}。订单、支付和任务都只对你本人可见。`
      : "未登录。登录后订单、支付和任务都会绑定到你的账号。"
  );
}

function renderPricingReceipt(order = state.currentOrder) {
  const estimate = state.currentEstimate || estimateDocument();
  if (!estimate || !order) return;

  els.pricingModalSubtitle.textContent = `订单状态：${order.status}。支付完成后才允许开始转换。`;
  els.pricingReceipt.innerHTML = `
    <div class="receipt-paper">
      <div class="receipt-row receipt-row-hero">
        <div>
          <span class="receipt-label">订单类型</span>
          <strong>LaTeX 论文转换</strong>
        </div>
        <div class="receipt-amount">${formatMoney(order.amount, order.currency)}</div>
      </div>
      <div class="receipt-divider"></div>
      <div class="receipt-grid">
        <span>服务商</span><strong>${estimate.provider.label}</strong>
        <span>模型</span><strong>${estimate.model.label}</strong>
        <span>预估字数</span><strong>${formatInteger(estimate.estimatedChars)}</strong>
        <span>输入 Tokens</span><strong>${formatInteger(estimate.inputTokens)}</strong>
        <span>输出 Tokens</span><strong>${formatInteger(estimate.outputTokens)}</strong>
        <span>输入成本</span><strong>${formatMoney(order.quote.input_cost, order.currency)}</strong>
        <span>输出成本</span><strong>${formatMoney(order.quote.output_cost, order.currency)}</strong>
        <span>模型原始成本</span><strong>${formatMoney(order.quote.raw_cost, order.currency)}</strong>
        <span>默认溢价倍率</span><strong>${Number(order.quote.markup_multiplier).toFixed(2)}x</strong>
        <span>最低收费</span><strong>${formatMoney(order.quote.minimum_price, order.currency)}</strong>
        <span>订单状态</span><strong>${order.status}</strong>
      </div>
      <div class="receipt-divider"></div>
      <div class="receipt-row receipt-row-total">
        <span>应付金额</span>
        <strong>${formatMoney(order.amount, order.currency)}</strong>
      </div>
      <p class="receipt-note">当前用模拟支付代替真实微信/支付宝回调。等接入正式支付后，这里会变成扫码和回调确认。</p>
    </div>
  `;
  els.wechatPayButton.disabled = state.paymentBusy || order.status !== "awaiting_payment";
  els.alipayButton.disabled = state.paymentBusy || order.status !== "awaiting_payment";
}

function invalidateOrder() {
  state.currentOrder = null;
  renderInlinePricing();
}

async function ensureOrderQuote() {
  if (!state.currentUser) {
    throw new Error("请先登录后再生成订单。");
  }
  const estimate = estimateDocument();
  if (!estimate || estimate.estimatedChars <= 0) {
    throw new Error("请先上传可识别的 LaTeX 压缩包。");
  }
  if (
    state.currentOrder &&
    state.currentOrder.provider === estimate.provider.id &&
    state.currentOrder.model === estimate.model.id &&
    Number(state.currentOrder.estimated_chars) === estimate.estimatedChars &&
    ["awaiting_payment", "paid", "processing", "completed"].includes(state.currentOrder.status)
  ) {
    return state.currentOrder;
  }

  const response = await fetch("/api/orders/quote", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: estimate.provider.id,
      model: estimate.model.id,
      estimated_chars: estimate.estimatedChars,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || data.detail || "生成订单失败");
  }
  state.currentOrder = data.order;
  renderInlinePricing();
  return state.currentOrder;
}

async function openPricingModal() {
  const order = await ensureOrderQuote();
  renderPricingReceipt(order);
  els.pricingModal.hidden = false;
  document.body.classList.add("modal-open");
}

async function submitAuth(path) {
  state.authBusy = true;
  renderAuthState("正在提交账号请求...");
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: els.authEmailInput.value,
        password: els.authPasswordInput.value,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || data.detail || "账号请求失败");
    }
    state.currentUser = data.user;
    els.authPasswordInput.value = "";
    invalidateOrder();
    renderAuthState();
    renderPapers();
  } catch (error) {
    renderAuthState(error.message || "账号请求失败");
  } finally {
    state.authBusy = false;
    renderAuthState();
  }
}

async function loadCurrentUser() {
  try {
    const response = await fetch("/api/auth/me");
    const data = await response.json();
    state.currentUser = data.user || null;
    renderAuthState();
    renderPapers();
  } catch {
    state.currentUser = null;
    renderAuthState("无法读取登录状态。");
    renderPapers();
  }
}

async function logout() {
  state.authBusy = true;
  renderAuthState("正在退出...");
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } finally {
    state.currentUser = null;
    state.currentOrder = null;
    state.authBusy = false;
    renderAuthState();
    renderPapers();
    renderInlinePricing();
  }
}

function closePricingModal() {
  els.pricingModal.hidden = true;
  document.body.classList.remove("modal-open");
}

function populateModelOptions(providerId, preferredModel) {
  const provider = getProviderInfo(providerId);
  if (!provider) return;

  els.modelSelect.innerHTML = "";
  provider.models.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = model.label;
    els.modelSelect.appendChild(option);
  });
  const nextModel = provider.models.some((model) => model.id === preferredModel)
    ? preferredModel
    : provider.models[0]?.id;
  if (nextModel) {
    els.modelSelect.value = nextModel;
  }
  renderInlinePricing();
}

function populateProviderOptions(defaults) {
  els.providerSelect.innerHTML = "";
  (state.catalog?.providers || []).forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider.id;
    option.textContent = provider.label;
    els.providerSelect.appendChild(option);
  });
  const preferredProvider = state.catalog?.providers?.some((provider) => provider.id === defaults?.provider)
    ? defaults.provider
    : state.catalog?.providers?.[0]?.id;
  if (!preferredProvider) return;
  els.providerSelect.value = preferredProvider;
  populateModelOptions(preferredProvider, defaults?.model);
}

async function loadCatalog() {
  try {
    const response = await fetch("/api/catalog");
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || data.detail || "模型目录加载失败");
    }
    state.catalog = data;
    populateProviderOptions(data.defaults);
  } catch (error) {
    els.pricingInline.innerHTML = `<p class="field-note is-error">${error.message || "模型目录加载失败"}</p>`;
  }
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function decodeFileName(bytes) {
  try {
    return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
  } catch {
    return Array.from(bytes, (byte) => String.fromCharCode(byte)).join("");
  }
}

function decodeTarString(bytes) {
  const nulIndex = bytes.indexOf(0);
  const slice = nulIndex >= 0 ? bytes.slice(0, nulIndex) : bytes;
  return decodeFileName(slice).trim();
}

function parseTarOctal(bytes) {
  const text = decodeTarString(bytes).replace(/\0/g, "").trim();
  return text ? Number.parseInt(text, 8) || 0 : 0;
}

function findEndOfCentralDirectory(view) {
  const minOffset = Math.max(0, view.byteLength - 65557);
  for (let offset = view.byteLength - 22; offset >= minOffset; offset -= 1) {
    if (view.getUint32(offset, true) === 0x06054b50) {
      return offset;
    }
  }
  return -1;
}

async function listZipEntries(file) {
  if (!file.name.toLowerCase().endsWith(".zip")) {
    throw new Error("文件不是 zip 格式");
  }

  const buffer = await file.arrayBuffer();
  if (buffer.byteLength === 0) {
    return [];
  }

  const view = new DataView(buffer);
  const eocd = findEndOfCentralDirectory(view);
  if (eocd < 0) {
    throw new Error("无法读取 zip 目录");
  }

  const totalEntries = view.getUint16(eocd + 10, true);
  const centralDirectoryOffset = view.getUint32(eocd + 16, true);
  const entries = [];
  let offset = centralDirectoryOffset;

  for (let index = 0; index < totalEntries; index += 1) {
    if (offset + 46 > view.byteLength || view.getUint32(offset, true) !== 0x02014b50) {
      throw new Error("zip 目录结构异常");
    }

    const nameLength = view.getUint16(offset + 28, true);
    const extraLength = view.getUint16(offset + 30, true);
    const commentLength = view.getUint16(offset + 32, true);
    const nameStart = offset + 46;
    const nameEnd = nameStart + nameLength;
    const nameBytes = new Uint8Array(buffer, nameStart, nameLength);
    const name = decodeFileName(nameBytes).replaceAll("\\", "/");
    const size = view.getUint32(offset + 24, true);
    entries.push({ name, size, isDir: name.endsWith("/") });
    offset = nameEnd + extraLength + commentLength;
  }

  return entries.filter((entry) => entry?.name);
}

async function listTarEntries(file) {
  const buffer = await file.arrayBuffer();
  if (buffer.byteLength === 0) {
    return [];
  }

  const bytes = new Uint8Array(buffer);
  const entries = [];
  let offset = 0;

  while (offset + 512 <= bytes.length) {
    const header = bytes.slice(offset, offset + 512);
    if (header.every((byte) => byte === 0)) {
      break;
    }

    const rawName = decodeTarString(header.slice(0, 100));
    const prefix = decodeTarString(header.slice(345, 500));
    const size = parseTarOctal(header.slice(124, 136));
    const typeflag = String.fromCharCode(header[156] || 48);
    const name = [prefix, rawName].filter(Boolean).join("/").replaceAll("\\", "/");

    if (name && typeflag !== "5") {
      entries.push({ name, size, isDir: false });
    } else if (name && typeflag === "5") {
      entries.push({ name: name.endsWith("/") ? name : `${name}/`, size: 0, isDir: true });
    }

    offset += 512 + Math.ceil(size / 512) * 512;
  }

  return entries.filter((entry) => entry?.name);
}

function isSupportedArchive(file) {
  const name = file.name.toLowerCase();
  return name.endsWith(".zip") || name.endsWith(".tar") || name.endsWith(".tar.gz") || name.endsWith(".tgz");
}

function validateEntries(entries) {
  const files = entries.filter((entry) => !entry.isDir);
  const warnings = [];
  const hasTex = files.some((entry) => entry.name.toLowerCase().endsWith(".tex"));
  const hasReferences = files.some((entry) => /\.(bib|bbl)$/i.test(entry.name));
  const hiddenOnly = files.length > 0 && files.every((entry) => /(^|\/)(__macosx|\.ds_store)/i.test(entry.name));

  if (files.length === 0 || hiddenOnly) {
    warnings.push({ level: "danger", text: "压缩包为空" });
  }
  if (!hasTex) {
    warnings.push({ level: "danger", text: "缺少 .tex 文件" });
  }
  if (!hasReferences) {
    warnings.push({ level: "warn", text: "未发现 .bib 或 .bbl" });
  }

  return {
    files,
    warnings,
    summary: files.length ? `${files.length} 个文件` : "未发现文件",
  };
}

function autoChapterFor(index) {
  const used = new Set(
    state.papers
      .map((paper) => paper.chapter)
      .filter(Boolean)
      .map(Number),
  );
  if (!used.size) return String(index + 1);
  for (let chapter = 1; chapter <= MAX_PAPERS; chapter += 1) {
    if (!used.has(chapter)) return String(chapter);
  }
  return "";
}

async function addFiles(fileList) {
  const candidates = Array.from(fileList).filter(isSupportedArchive);
  const remainingSlots = MAX_PAPERS - state.papers.length;
  const selected = candidates.slice(0, remainingSlots);
  if (selected.length) {
    invalidateOrder();
  }

  for (const file of selected) {
    const paper = {
      id: crypto.randomUUID(),
      file,
      chapter: "",
      status: "checking",
      summary: "正在检查压缩包结构",
      warnings: [],
      entries: [],
      terms: [],
      termsOpen: false,
    };
    state.papers.push(paper);
    renderPapers();

    try {
      const lowerName = file.name.toLowerCase();
      const entries = lowerName.endsWith(".zip") ? await listZipEntries(file) : await listTarEntries(file);
      const validation = validateEntries(entries);
      paper.status = "ready";
      paper.entries = entries;
      paper.summary = validation.summary;
      paper.warnings = validation.warnings;
    } catch (error) {
      paper.status = "error";
      paper.summary = "读取失败";
      paper.warnings = [{ level: "danger", text: error.message || "压缩包检查失败" }];
    }
    renderPapers();
  }
}

function getEffectiveChapters() {
  const used = new Set();
  return state.papers.map((paper, index) => {
    const explicit = paper.chapter ? Number(paper.chapter) : null;
    if (explicit && !used.has(explicit)) {
      used.add(explicit);
      return explicit;
    }

    for (let chapter = 1; chapter <= MAX_PAPERS; chapter += 1) {
      if (!used.has(chapter)) {
        used.add(chapter);
        return chapter;
      }
    }
    return index + 1;
  });
}

function hasBlockingIssues() {
  if (!state.papers.length) return true;
  const duplicateChapters = new Set();
  const seen = new Set();
  for (const paper of state.papers) {
    if (paper.chapter && seen.has(paper.chapter)) duplicateChapters.add(paper.chapter);
    if (paper.chapter) seen.add(paper.chapter);
  }

  return (
    duplicateChapters.size > 0 ||
    state.papers.some((paper) => paper.status === "checking" || paper.warnings.some((warning) => warning.level === "danger"))
  );
}

function renderPapers() {
  const chapters = getEffectiveChapters();
  els.paperCounter.textContent = `${state.papers.length} / ${MAX_PAPERS}`;
  els.paperList.innerHTML = "";

  state.papers.forEach((paper, index) => {
    const item = els.template.content.firstElementChild.cloneNode(true);
    const title = item.querySelector("h4");
    const meta = item.querySelector("p");
    const warnings = item.querySelector(".warnings");
    const select = item.querySelector(".chapter-select");
    const removeButton = item.querySelector(".remove-paper");
    const termsToggle = item.querySelector(".terms-toggle");
    const addTermButton = item.querySelector(".add-term");
    const termsBody = item.querySelector(".terms-body");
    const termsList = item.querySelector(".term-list");
    const termsCount = item.querySelector(".terms-count");

    title.textContent = paper.file.name;
    meta.textContent = `${formatBytes(paper.file.size)} · 第 ${chapters[index]} 章 · ${paper.summary}`;
    select.value = paper.chapter;
    item.classList.toggle("terms-open", paper.termsOpen);
    termsToggle.setAttribute("aria-expanded", String(paper.termsOpen));
    termsCount.textContent = paper.terms.length;

    select.addEventListener("change", () => {
      paper.chapter = select.value;
      renderPapers();
    });
    removeButton.addEventListener("click", () => {
      invalidateOrder();
      state.papers = state.papers.filter((candidate) => candidate.id !== paper.id);
      renderPapers();
    });
    termsToggle.addEventListener("click", () => {
      paper.termsOpen = !paper.termsOpen;
      renderPapers();
    });
    addTermButton.addEventListener("click", () => {
      paper.termsOpen = true;
      paper.terms.push({ source: "", target: "" });
      state.focusTerm = { paperId: paper.id, termIndex: paper.terms.length - 1 };
      renderPapers();
    });

    renderPaperTerms(paper, termsList);

    if (paper.status === "checking") {
      warnings.appendChild(makePill("正在检查", "warn"));
    } else if (paper.warnings.length) {
      paper.warnings.forEach((warning) => warnings.appendChild(makePill(warning.text, warning.level)));
    } else {
      warnings.appendChild(makePill("结构检查通过", "ok"));
    }

    const duplicate = paper.chapter && state.papers.filter((candidate) => candidate.chapter === paper.chapter).length > 1;
    if (duplicate) warnings.appendChild(makePill(`章号 ${paper.chapter} 重复`, "danger"));

    els.paperList.appendChild(item);
  });

  focusPendingTerm();
  els.pricingButton.disabled = !state.currentUser || hasBlockingIssues();
  els.startButton.disabled = hasBlockingIssues() || state.jobState === "running" || state.currentOrder?.status !== "paid";
  els.dropHint.textContent = state.papers.length ? "你可以继续上传，最多十篇。" : "或点击选择文件。";
  renderInlinePricing();
}

function renderPaperTerms(paper, termsList) {
  termsList.innerHTML = "";

  paper.terms.forEach((term, termIndex) => {
    const row = document.createElement("div");
    row.className = "term-row";

    const sourceLabel = document.createElement("label");
    sourceLabel.className = "term-field";
    const sourceText = document.createElement("span");
    sourceText.textContent = "英文";
    const sourceInput = document.createElement("input");
    sourceInput.dataset.paperId = paper.id;
    sourceInput.dataset.termIndex = String(termIndex);
    sourceInput.dataset.termField = "source";
    sourceInput.value = term.source;
    sourceInput.placeholder = "attention";
    sourceInput.autocomplete = "off";
    sourceInput.addEventListener("input", () => {
      term.source = sourceInput.value;
    });
    sourceLabel.append(sourceText, sourceInput);

    const targetLabel = document.createElement("label");
    targetLabel.className = "term-field";
    const targetText = document.createElement("span");
    targetText.textContent = "中文";
    const targetInput = document.createElement("input");
    targetInput.value = term.target;
    targetInput.placeholder = "注意力";
    targetInput.autocomplete = "off";
    targetInput.addEventListener("input", () => {
      term.target = targetInput.value;
    });
    targetLabel.append(targetText, targetInput);

    const remove = document.createElement("button");
    remove.className = "icon-button term-remove";
    remove.type = "button";
    remove.setAttribute("aria-label", "删除术语");
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      paper.terms.splice(termIndex, 1);
      renderPapers();
    });

    row.append(sourceLabel, targetLabel, remove);
    termsList.appendChild(row);
  });
}

function focusPendingTerm() {
  if (!state.focusTerm) return;
  const selector = `[data-paper-id="${state.focusTerm.paperId}"][data-term-index="${state.focusTerm.termIndex}"][data-term-field="source"]`;
  const input = document.querySelector(selector);
  if (input) {
    input.focus();
  }
  state.focusTerm = null;
}

function makePill(text, level) {
  const pill = document.createElement("span");
  pill.className = level === "ok" ? "ok-pill" : `warning-pill ${level === "danger" ? "is-danger" : ""}`;
  pill.textContent = text;
  return pill;
}

function displayMessage(message, stage) {
  return messageTranslations[message] || stageTranslations[stage] || message || "正在处理";
}

function collectTermsByChapter(chapters) {
  const termsByChapter = {};
  state.papers.forEach((paper, index) => {
    const chapter = String(chapters[index]);
    paper.terms.forEach((term) => {
      const source = term.source.trim();
      const target = term.target.trim();
      if (!source || !target) return;
      termsByChapter[chapter] ||= {};
      termsByChapter[chapter][source] = target;
    });
  });
  return termsByChapter;
}

function buildJobPayload() {
  const chapters = getEffectiveChapters();
  return {
    papers: state.papers.map((paper, index) => ({
      fileName: paper.file.name,
      chapter: chapters[index],
      zipEntries: paper.entries.map((entry) => entry.name),
    })),
    order_id: state.currentOrder?.id || null,
    translate: {
      provider: els.providerSelect.value,
      model: els.modelSelect.value,
    },
    estimate: {
      estimated_chars: state.currentEstimate?.estimatedChars || 0,
    },
    rendering: {},
    terms: collectTermsByChapter(chapters),
  };
}

function setProgress(percent, stage, status = "running") {
  const bounded = Math.max(0, Math.min(100, percent));
  els.progressFill.style.width = `${bounded}%`;
  els.progressLabel.textContent = `${bounded}%`;
  els.stageLabel.textContent = stage;
  els.statusDot.className = `status-dot ${status === "running" ? "is-running" : status === "done" ? "is-done" : ""}`;
  els.statusText.textContent = status === "done" ? "转换完成" : "转换中";
}

function setChunkProgress(current, total) {
  const done = Number(current);
  const all = Number(total);
  if (!Number.isFinite(done) || !Number.isFinite(all) || all <= 0) {
    els.chunkProgress.hidden = true;
    return;
  }
  els.chunkProgress.hidden = false;
  els.chunkProgress.textContent = `翻译进度：${Math.min(done, all)} / ${all} chunks`;
}

async function startJob() {
  if (!state.currentUser) {
    els.stageLabel.textContent = "请先登录";
    return;
  }
  if (!state.currentOrder || state.currentOrder.status !== "paid") {
    els.stageLabel.textContent = "请先完成支付后再开始转换";
    return;
  }
  const payload = buildJobPayload();
  state.jobState = "running";
  els.startButton.disabled = true;
  els.downloadLink.classList.add("is-disabled");
  els.downloadLink.setAttribute("aria-disabled", "true");
  els.downloadLink.href = "#";
  setProgress(0, "上传任务", "running");
  setChunkProgress(null, null);

  const form = new FormData();
  form.append("payload", JSON.stringify(payload));
  state.papers.forEach((paper) => form.append("papers", paper.file, paper.file.name));

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: form,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || data.detail || "任务创建失败");
    }
    state.jobId = data.job.id;
    state.eventOffset = 0;
    handleJobStatus(data.job);
    subscribeToEvents(state.jobId);
  } catch (error) {
    state.jobState = "failed";
    els.statusDot.className = "status-dot";
    els.statusText.textContent = "创建失败";
    els.stageLabel.textContent = error.message || "任务创建失败";
    els.startButton.disabled = hasBlockingIssues();
  }
}

function resetAll() {
  window.clearInterval(state.pollTimer);
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  state.papers = [];
  state.jobState = "idle";
  state.jobId = null;
  state.eventOffset = 0;
  state.currentOrder = null;
  state.currentEstimate = null;
  state.paymentBusy = false;
  els.fileInput.value = "";
  els.progressFill.style.width = "0%";
  els.progressLabel.textContent = "0%";
  els.stageLabel.textContent = "未开始";
  setChunkProgress(null, null);
  els.statusDot.className = "status-dot";
  els.statusText.textContent = "等待上传";
  els.downloadLink.classList.add("is-disabled");
  els.downloadLink.setAttribute("aria-disabled", "true");
  els.downloadLink.href = "#";
  closePricingModal();
  renderPapers();
  renderInlinePricing();
}

function handleJobStatus(job) {
  if (!job) return;
  const stateName = job.state || "running";
  const percent = Number(job.percent || 0);
  const message = displayMessage(job.message, job.stage);
  if (stateName === "completed") {
    state.jobState = "done";
    if (state.currentOrder) state.currentOrder.status = "completed";
    setProgress(100, "转换完成", "done");
    if (!els.chunkProgress.hidden) {
      const match = els.chunkProgress.textContent.match(/(\d+)\s*\/\s*(\d+)/);
      if (match) setChunkProgress(match[2], match[2]);
    }
    els.startButton.disabled = hasBlockingIssues();
    els.downloadLink.classList.remove("is-disabled");
    els.downloadLink.setAttribute("aria-disabled", "false");
    els.downloadLink.href = `/api/jobs/${job.id || state.jobId}/download`;
    renderInlinePricing();
    return;
  }
  if (stateName === "failed") {
    state.jobState = "failed";
    if (state.currentOrder) state.currentOrder.status = "failed";
    els.statusDot.className = "status-dot";
    els.statusText.textContent = "转换失败";
    els.progressFill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    els.progressLabel.textContent = `${Math.round(percent)}%`;
    els.stageLabel.textContent = message;
    els.startButton.disabled = hasBlockingIssues();
    renderInlinePricing();
    return;
  }
  if (state.currentOrder && stateName === "running") {
    state.currentOrder.status = "processing";
  }
  setProgress(Math.round(percent), message, "running");
  renderInlinePricing();
}

function handleEvent(event) {
  if (event.offset) {
    state.eventOffset = Math.max(state.eventOffset, Number(event.offset));
  }
  const percent = Number(event.percent || 0);
  if (event.stage === "translate" && event.total) {
    setChunkProgress(event.current || 0, event.total);
  }
  if (event.state === "completed" || event.stage === "done") {
    handleJobStatus({ id: state.jobId, state: "completed", percent: 100, message: event.message });
  } else if (event.state === "failed" || event.stage === "failed") {
    handleJobStatus({ id: state.jobId, state: "failed", percent, message: displayMessage(event.message, event.stage) });
  } else if (event.message) {
    setProgress(Math.round(percent), displayMessage(event.message, event.stage), "running");
  }
}

function switchPage(page) {
  els.pages.forEach((panel) => panel.classList.toggle("is-active", panel.dataset.pagePanel === page));
}

function subscribeToEvents(jobId) {
  if (state.eventSource) state.eventSource.close();
  if (window.EventSource) {
    state.eventSource = new EventSource(`/api/jobs/${jobId}/events`);
    state.eventSource.onmessage = (message) => {
      const event = JSON.parse(message.data);
      handleEvent(event);
    };
    state.eventSource.onerror = () => {
      state.eventSource.close();
      state.eventSource = null;
      startPolling(jobId);
    };
  } else {
    startPolling(jobId);
  }
}

function startPolling(jobId) {
  window.clearInterval(state.pollTimer);
  state.pollTimer = window.setInterval(async () => {
    try {
      const response = await fetch(`/api/jobs/${jobId}/events?after=${state.eventOffset}`);
      const data = await response.json();
      (data.events || []).forEach(handleEvent);
      handleJobStatus(data.job);
      if (["completed", "failed", "cancelled"].includes(data.job?.state)) {
        window.clearInterval(state.pollTimer);
      }
    } catch {
      els.stageLabel.textContent = "正在重连任务事件";
    }
  }, 1400);
}

async function mockPay(channel) {
  if (!state.currentOrder) {
    els.pricingModalSubtitle.textContent = "请先生成订单后再支付。";
    return;
  }
  state.paymentBusy = true;
  renderPricingReceipt(state.currentOrder);
  try {
    const response = await fetch(`/api/orders/${state.currentOrder.id}/mock-pay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payment_channel: channel }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || data.detail || "支付失败");
    }
    state.currentOrder = data.order;
    renderPricingReceipt(state.currentOrder);
    renderInlinePricing();
    els.stageLabel.textContent = "支付成功，可以开始转换";
  } catch (error) {
    els.pricingModalSubtitle.textContent = error.message || "支付失败";
  } finally {
    state.paymentBusy = false;
    if (state.currentOrder) {
      renderPricingReceipt(state.currentOrder);
    }
  }
}

els.guideStartButton.addEventListener("click", () => {
  switchPage("workspace");
});

els.homeButton.addEventListener("click", () => {
  switchPage("guide");
});

["dragenter", "dragover"].forEach((eventName) => {
  els.dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    els.dropzone.classList.add("is-dragover");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  els.dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    els.dropzone.classList.remove("is-dragover");
  });
});

els.dropzone.addEventListener("drop", (event) => {
  addFiles(event.dataTransfer.files);
});

els.fileInput.addEventListener("change", (event) => {
  addFiles(event.target.files);
});

els.providerSelect.addEventListener("change", () => {
  invalidateOrder();
  populateModelOptions(els.providerSelect.value);
});
els.modelSelect.addEventListener("change", () => {
  invalidateOrder();
  renderInlinePricing();
});
els.loginButton.addEventListener("click", () => {
  submitAuth("/api/auth/login");
});
els.registerButton.addEventListener("click", () => {
  submitAuth("/api/auth/register");
});
els.logoutButton.addEventListener("click", logout);
els.pricingButton.addEventListener("click", async () => {
  try {
    await openPricingModal();
  } catch (error) {
    els.stageLabel.textContent = error.message || "生成报价失败";
  }
});
els.pricingModalClose.addEventListener("click", closePricingModal);
els.pricingModal.addEventListener("click", (event) => {
  if (event.target instanceof HTMLElement && event.target.hasAttribute("data-close-modal")) {
    closePricingModal();
  }
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !els.pricingModal.hidden) {
    closePricingModal();
  }
});
els.wechatPayButton.addEventListener("click", () => mockPay("wechat"));
els.alipayButton.addEventListener("click", () => mockPay("alipay"));
els.startButton.addEventListener("click", startJob);
els.resetButton.addEventListener("click", resetAll);

renderPapers();
closePricingModal();
loadCurrentUser();
loadCatalog();
